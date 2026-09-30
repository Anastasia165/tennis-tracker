import io
import tempfile
import unittest
from datetime import date
from pathlib import Path

from bot import Tracker, parse_date, parse_minutes, parse_money
from reports import chart, money, summary
from store import Store

TODAY = date(2026, 9, 19)


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.bot = Tracker(self.store, 42, lambda: TODAY)
        self.counter = 0

    def tearDown(self):
        self.store.close()

    def send(self, text, user=42, chat_type="private", update_id=None):
        self.counter += 1
        update_id = update_id if update_id is not None else self.counter
        self.bot.handle({
            "update_id": update_id,
            "message": {
                "text": text, "from": {"id": user},
                "chat": {"id": user, "type": chat_type},
            },
        })
        return update_id

    def add(self, command="/add 19.09.2026 90 2500"):
        self.send(command)
        self.send("4")
        self.send("Анна")
        return self.send("Сохранить")

    def test_confirmation_required(self):
        self.send("/add 19.09.2026 90 2500")
        self.assertEqual(self.store.history(42), [])
        self.send("4")
        self.send("Анна")
        self.send("Сохранить")
        row = self.store.history(42)[0]
        self.assertEqual((row["minutes"], row["cost_kopecks"]), (90, 250000))

    def test_guided_input(self):
        for value in ["Записать тренировку", "Вчера", "60", "1800,50", "2", "Анна", "Сохранить"]:
            self.send(value)
        row = self.store.history(42)[0]
        self.assertEqual((row["played_on"], row["minutes"], row["cost_kopecks"]),
                         ("2026-09-18", 60, 180050))
        self.assertIsNone(self.store.draft(42))

    def test_duplicate_update_and_double_confirm_do_not_duplicate_record(self):
        update_id = self.add()
        replies = self.store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        self.send("Сохранить", update_id=update_id)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], replies)
        self.send("Сохранить")
        self.assertEqual(len(self.store.history(42)), 1)

    def test_invalid_cost_retains_draft(self):
        for value in ["/add", "Сегодня", "90", "не знаю"]:
            self.send(value)
        self.assertEqual(self.store.draft(42)["step"], "cost")
        self.send("0")
        self.send("Не помню")
        self.send("Без тренера")
        self.send("Сохранить")
        self.assertEqual(self.store.history(42)[0]["cost_kopecks"], 0)

    def test_cancel(self):
        self.send("/add 19.09.2026 90 2500")
        self.send("/cancel")
        self.send("Сохранить")
        self.assertEqual(self.store.history(42), [])

    def test_owner_only_private_chats(self):
        with self.assertLogs(level="WARNING") as logs:
            self.send("/add 19.09.2026 90 2500", user=99)
            self.send("Сохранить", user=99)
            self.send("/month", user=99)
            self.send("/add 19.09.2026 90 2500", chat_type="group")
        output = "\n".join(logs.output)
        self.assertIn("sender_id=99, TELEGRAM_USER_ID=42", output)
        self.assertNotIn("2500", output)
        self.assertEqual(self.store.history(99), [])
        self.assertIsNone(self.store.draft(42))
        self.assertIsNone(self.store.pending())

    def test_corrected_owner_id_allows_new_start(self):
        with self.assertLogs(level="WARNING"):
            self.send("/start", user=99)
        self.assertIsNone(self.store.pending())
        self.bot = Tracker(self.store, 99, lambda: TODAY)
        self.send("/start", user=99)
        reply = self.store.pending()
        self.assertEqual(reply["chat_id"], 99)
        self.assertIn("/add", reply["payload"]["text"])

    def test_month_boundaries_and_totals(self):
        self.add("/add 31.08.2026 30 1000")
        self.add("/add 01.09.2026 60 2000")
        self.add("/add 19.09.2026 90 2500")
        rows = self.store.month(42, "2026-09")
        result = summary(rows, "2026-09")
        self.assertIn("Тренировок: 2", result)
        self.assertIn("2 ч 30 мин", result)
        self.assertIn("4 500,00 ₽", result)
        self.assertIn("2 250,00 ₽", result)
        self.assertIn("1 800,00 ₽", result)
        self.assertEqual(self.store.month(99, "2026-09"), [])

    def test_delete_needs_confirmation_and_updates_totals(self):
        self.add()
        row_id = self.store.history(42)[0]["id"]
        self.send(f"/delete {row_id}")
        self.assertEqual(len(self.store.history(42)), 1)
        update_id = self.send("Удалить")
        self.send("Удалить", update_id=update_id)
        self.assertEqual(self.store.month(42, "2026-09"), [])
        self.assertEqual(self.store.history(42), [])

    def test_delete_cannot_target_other_user(self):
        with self.store.db:
            other_id = self.store.add(99, {"date": "2026-09-19", "minutes": 60, "cost": 100})
        self.send(f"/delete {other_id}")
        self.assertIsNone(self.store.draft(42))
        self.assertEqual(len(self.store.history(99)), 1)

    def test_empty_month_does_not_queue_chart(self):
        self.send("/month 2026-08")
        self.assertEqual(self.store.pending()["kind"], "text")
        self.assertIn("тренировок пока нет", self.store.pending()["payload"]["text"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)

    def test_report_queues_text_and_chart(self):
        self.add()
        self.send("/month")
        kinds = [row[0] for row in self.store.db.execute("SELECT kind FROM outbox")]
        self.assertEqual(kinds[-2:], ["text", "chart"])

    def test_future_training_rejected(self):
        self.send("/add 20.09.2026 90 2500")
        self.assertIsNone(self.store.draft(42))
        self.assertEqual(self.store.history(42), [])

    def test_invalid_month_keeps_bot_responsive(self):
        for text in ["/month 2026-13", "/month 2026-10", "/month aaa", "/delete 99999999999999999999999999"]:
            self.send(text)
        self.add()
        self.assertEqual(len(self.store.history(42)), 1)

    def test_transaction_rolls_back_state_and_marker_on_failure(self):
        original = self.store.enqueue
        def fail(*args):
            raise RuntimeError("simulated storage error")
        self.store.enqueue = fail
        with self.assertRaises(RuntimeError):
            self.send("/add", update_id=100)
        self.store.enqueue = original
        self.assertFalse(self.store.processed(100))
        self.assertIsNone(self.store.draft(42))
        self.send("/add", update_id=100)
        self.assertEqual(self.store.draft(42)["step"], "date")


class ParsingAndStorageTests(unittest.TestCase):
    def test_money_is_exact_and_finite(self):
        self.assertEqual(parse_money("0,29"), 29)
        self.assertEqual(parse_money("2500.50"), 250050)
        for value in ["NaN", "Infinity", "-1", "1.999", "1e3", "1x20", "1000001", ""]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_money(value)
        self.assertEqual(money(29), "0,29 ₽")

    def test_minutes_and_dates(self):
        for value in ["0", "-1", "1.5", "1441", "NaN"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_minutes(value)
        self.assertEqual(parse_date("Вчера", date(2026, 1, 1)), "2025-12-31")
        self.assertEqual(parse_date("29.02.2024", TODAY), "2024-02-29")
        with self.assertRaises(ValueError):
            parse_date("29.02.2026", TODAY)

    def test_restart_and_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tracker.sqlite3"
            store = Store(path)
            bot = Tracker(store, 42, lambda: TODAY)
            bot.handle({
                "update_id": 5,
                "message": {"text": "/add", "from": {"id": 42}, "chat": {"id": 42, "type": "private"}},
            })
            store.close()
            store = Store(path)
            try:
                self.assertEqual(store.draft(42)["step"], "date")
                self.assertEqual(store.offset(), 6)
                self.assertTrue(store.processed(5))
                self.assertIsNotNone(store.pending())
                backup = Path(directory) / "backup.sqlite3"
                store.backup(backup)
                restored = Store(backup)
                try:
                    self.assertEqual(restored.draft(42)["step"], "date")
                finally:
                    restored.close()
                with self.assertRaises(FileExistsError):
                    store.backup(backup)
            finally:
                store.close()

    def test_chart_png(self):
        from PIL import Image
        rows = [
            {"played_on": "2026-09-01", "minutes": 60, "cost_kopecks": 150000},
            {"played_on": "2026-09-01", "minutes": 90, "cost_kopecks": 250000},
            {"played_on": "2026-09-19", "minutes": 90, "cost_kopecks": 0},
        ]
        result = chart(rows, "2026-09", TODAY)
        self.assertTrue(result.startswith(b"\x89PNG"))
        with Image.open(io.BytesIO(result)) as image:
            self.assertGreater(image.width, 900)
            self.assertGreater(image.height, 900)


class TelegramTransportTests(unittest.TestCase):
    def test_json_request(self):
        import json
        from unittest.mock import patch
        from bot import Telegram
        response = io.BytesIO(b'{"ok":true,"result":[]}')
        with patch("bot.urlopen", return_value=response) as request:
            result = Telegram("123:test").call("getUpdates", {"offset": 7})
        self.assertEqual(result, [])
        self.assertEqual(json.loads(request.call_args.args[0].data), {"offset": 7})

    def test_photo_upload(self):
        from unittest.mock import patch
        from bot import Telegram
        response = io.BytesIO(b'{"ok":true,"result":{}}')
        with patch("bot.urlopen", return_value=response) as request:
            Telegram("123:test").call("sendPhoto", {"chat_id": 42}, photo=b"PNG-BYTES")
        body = request.call_args.args[0].data
        self.assertIn(b'name="photo"; filename="report.png"', body)
        self.assertIn(b"Content-Type: image/png", body)
        self.assertIn(b"PNG-BYTES", body)
        self.assertIn(b'name="chat_id"', body)

    def test_network_error_does_not_expose_token(self):
        from unittest.mock import patch
        from urllib.error import URLError
        from bot import Telegram, APIError
        with patch("bot.urlopen", side_effect=URLError("https://api.telegram.org/bot123:secret")):
            with self.assertRaises(APIError) as result:
                Telegram("123:secret").call("getMe", {})
        self.assertNotIn("secret", str(result.exception))


if __name__ == "__main__":
    unittest.main()
