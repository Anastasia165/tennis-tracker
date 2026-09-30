import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from bot import Telegram, Tracker, parse_participants, session_text
from date_picker import picker
from store import Store


class NewFieldsTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.bot = Tracker(self.store, 42, lambda: date(2026, 9, 19))
        self.counter = 0

    def tearDown(self):
        self.store.close()

    def send(self, text):
        self.counter += 1
        self.bot.handle({"update_id": self.counter, "message": {
            "from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": text}})

    def callback(self, action, value="", token=None, user=42):
        self.counter += 1
        token = token or self.store.draft(42).get("calendar_token")
        update = {"update_id": self.counter, "callback_query": {
            "id": str(self.counter), "from": {"id": user},
            "message": {"message_id": 123, "chat": {"id": 42, "type": "private"}},
            "data": f"cal:{token}:{action}:{value}"}}
        self.bot.handle(update)
        return update

    def test_fields_roundtrip_and_coach_reuse(self):
        for text in ("/add", "Вчера", "90", "2500", "4", "Анна", "Сохранить"):
            self.send(text)
        row = self.store.history(42)[0]
        self.assertEqual((row["participants"], row["coach"]), (4, "Анна"))
        self.assertIn("Анна", session_text(row))
        self.assertEqual(self.store.coaches(42), ["Анна"])
        self.assertEqual(self.store.coaches(99), [])
        for text in ("/add 01.09.2026 60 1000", "2", "Тренер: Анна", "Сохранить"):
            self.send(text)
        self.assertEqual(self.store.coaches(42), ["Анна"])

    def test_validation_and_optional_fields(self):
        for value in ("0", "-1", "101", "2.5", "", "abc"):
            with self.assertRaises(ValueError):
                parse_participants(value)
        self.assertIsNone(parse_participants("Не помню"))
        for text in ("/add 01.09.2026 60 0", "0"):
            self.send(text)
        self.assertEqual(self.store.draft(42)["step"], "participants")
        for text in ("Не помню", "x" * 81):
            self.send(text)
        self.assertEqual(self.store.draft(42)["step"], "coach")
        self.send("Без тренера")
        self.send("Сохранить")
        row = self.store.history(42)[0]
        self.assertIsNone(row["participants"])
        self.assertEqual(row["coach"], "")

    def test_calendar_navigation_selection_and_duplicate(self):
        self.send("/add")
        self.send("Календарь")
        self.callback("month", "2024-02-01")
        payload = json.loads(self.store.db.execute(
            "SELECT payload FROM outbox WHERE kind='calendar' ORDER BY id DESC LIMIT 1").fetchone()[0])
        self.assertEqual(payload["message_id"], 123)
        self.assertIn("2024", payload["text"])
        update = self.callback("day", "2024-02-29")
        self.bot.handle(update)
        self.assertEqual(self.store.draft(42)["date"], "2024-02-29")
        self.assertEqual(self.store.draft(42)["step"], "minutes")

    def test_stale_foreign_future_and_malformed_callbacks(self):
        self.send("/add")
        self.send("Календарь")
        token = self.store.draft(42)["calendar_token"]
        self.callback("day", "2026-09-18", user=99)
        self.callback("day", "2026-09-20")
        self.callback("day", "bad")
        self.assertEqual(self.store.draft(42)["step"], "date")
        self.send("/cancel")
        self.send("/add")
        self.send("Календарь")
        self.callback("day", "2026-09-18", token=token)
        self.assertNotIn("date", self.store.draft(42))
        self.callback("day", "2026-09-18")
        self.assertEqual(self.store.draft(42)["date"], "2026-09-18")

    def test_recent_dates_cross_year_and_calendar_limits(self):
        payload = picker("abc", date(2026, 1, 1), date(2026, 1, 2), recent=True)
        buttons = [b for row in payload["reply_markup"]["inline_keyboard"] for b in row]
        self.assertEqual(len(buttons), 7)
        self.assertTrue(any("2025-12-31" in b["callback_data"] for b in buttons))
        payload = picker("abc", date(2000, 1, 1), date(2000, 1, 2))
        buttons = [b for row in payload["reply_markup"]["inline_keyboard"] for b in row]
        dates = [b["callback_data"].split(":")[-1] for b in buttons if ":day:" in b["callback_data"]]
        self.assertEqual(dates, ["2000-01-01", "2000-01-02"])
        self.assertFalse(any(":month:" in b["callback_data"] for b in buttons))
        self.assertTrue(all(len(b["callback_data"].encode()) <= 64 for b in buttons))

    def test_old_confirm_draft_can_still_save(self):
        with self.store.db:
            self.store.set_draft(42, {"step": "confirm", "date": "2026-09-01", "minutes": 90, "cost": 100})
        self.send("Сохранить")
        self.assertIsNone(self.store.history(42)[0]["participants"])


class MigrationAndTransportTests(unittest.TestCase):
    def test_old_schema_preserves_records_on_repeated_open(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "old.sqlite3"
            db = sqlite3.connect(path)
            db.executescript("""
                CREATE TABLE sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                    activity_id TEXT NOT NULL, played_on TEXT NOT NULL, minutes INTEGER NOT NULL,
                    cost_kopecks INTEGER NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                INSERT INTO sessions(user_id,activity_id,played_on,minutes,cost_kopecks)
                    VALUES(42,'tennis','2026-09-01',90,250000);
            """)
            db.close()
            for _ in range(2):
                store = Store(path)
                try:
                    row = store.history(42)[0]
                    self.assertEqual((row["id"], row["cost_kopecks"]), (1, 250000))
                    self.assertIsNone(row["participants"])
                    self.assertIsNone(row["coach"])
                finally:
                    store.close()

    def test_inline_transport_and_callback_ack(self):
        telegram = Telegram("123:fake")
        with patch.object(telegram, "call") as call:
            payload = picker("abc", date(2026, 9, 1), date(2026, 9, 19))
            telegram.deliver({"kind": "calendar", "chat_id": 42, "payload": payload})
            self.assertEqual(call.call_args.args[0], "sendMessage")
            self.assertIn("inline_keyboard", call.call_args.args[1]["reply_markup"])
            payload["message_id"] = 123
            telegram.deliver({"kind": "calendar", "chat_id": 42, "payload": payload})
            self.assertEqual(call.call_args.args[0], "editMessageText")
            telegram.deliver({"kind": "callback", "chat_id": 42,
                              "payload": {"callback_query_id": "query"}})
            call.assert_called_with("answerCallbackQuery", {"callback_query_id": "query"})


if __name__ == "__main__":
    unittest.main()
