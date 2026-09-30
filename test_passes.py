import io
import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from bot import Tracker, Telegram
from pass_ai import YandexRecognizer, RecognitionError, validate_result
from reports import summary, chart
from store import Store


class PassTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.recognizer = Mock()
        self.telegram = Mock()
        self.telegram.download_photo.return_value = b"fake jpeg"
        self.recognizer.ocr.return_value = {"text": "fake", "words": []}
        self.recognizer.extract.return_value = {
            "number": "465", "deposits": ["8000", "5000", "60"],
            "sessions": [{"date": "06.09", "cost": "700"}, {"date": "13.09", "cost": "1400"}],
            "notes": ["1250: уточнить, какое занятие исправлено"]}
        self.bot = Tracker(self.store, 42, lambda: date(2026, 9, 19),
                           recognizer=self.recognizer, telegram=self.telegram)
        self.counter = 0

    def tearDown(self):
        self.store.close()

    def send(self, text="", photo=None, user=42, chat_type="private"):
        self.counter += 1
        message = {"from": {"id": user}, "chat": {"id": user, "type": chat_type}, "text": text}
        if photo:
            message["photo"] = [{"file_id": photo, "file_unique_id": photo}]
        update = {"update_id": self.counter, "message": message}
        self.bot.handle(update)
        return update

    def series(self, *values):
        for value in values:
            self.send(value)

    def manual(self):
        self.series("/pass", "465", "2025", "8000 5000 60", "Не помню", "Анна",
                    "06.09", "700", "4")

    def job(self):
        row = self.store.db.execute("SELECT * FROM outbox WHERE kind='pass_ai' ORDER BY id DESC LIMIT 1").fetchone()
        return {**dict(row), "payload": json.loads(row["payload"])}

    def ai(self):
        self.send("/pass_ai")
        self.send(photo="front")
        self.send(photo="back")
        self.send("Распознать")
        self.bot.passes.process_job(self.job())

    def test_manual_confirm_and_money_no_double_count(self):
        self.manual()
        self.assertEqual(self.store.history(42), [])
        self.assertEqual(self.store.passes(42), [])
        update = self.send("Сохранить абонемент")
        self.bot.handle(update)
        self.send("Сохранить абонемент")
        rows = self.store.history(42)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["minutes"])
        self.assertEqual(rows[0]["participants"], 4)
        card = self.store.passes(42)[0]
        self.assertEqual((card["paid"], card["used"]), (1306000, 70000))
        report = summary(self.store.month(42, "2025-09"), "2025-09")
        self.assertIn("700,00", report)
        self.assertNotIn("13 060", report)
        self.assertIn("Длительность известна для 0 из 1", report)
        self.assertTrue(chart(rows, "2025-09").startswith(b"\x89PNG"))
        self.assertEqual(self.store.passes(99), [])

    def test_edit_row_deposits_and_remove(self):
        self.manual()
        self.series("Изменить занятие", "1", "Верно", "1250", "90", "2", "Без тренера")
        row = self.store.draft(42)["sessions"][0]
        self.assertEqual((row["cost"], row["minutes"], row["participants"], row["coach"]),
                         (125000, 90, 2, ""))
        self.series("Изменить пополнения", "1250", "Добавить занятие", "31.12.2024", "100", "4",
                    "Убрать занятие", "2", "Сохранить абонемент")
        card = self.store.passes(42)[0]
        self.assertEqual(card["paid"], card["used"])
        self.assertEqual(card["visits"], 1)

    def test_duplicate_warning_requires_explicit_decision(self):
        self.manual()
        with self.store.db:
            self.store.add(42, {"date": "2025-09-06", "minutes": 90, "cost": 70000})
        self.send("Сохранить абонемент")
        self.assertEqual(self.store.draft(42)["step"], "pass_duplicates")
        self.assertEqual(self.store.passes(42), [])
        self.send("Это разные занятия")
        self.assertEqual(len(self.store.history(42)), 2)

    def test_atomic_failure_and_cancel(self):
        self.manual()
        self.series("Добавить занятие", "13.09", "840", "3")
        original = self.store.add
        calls = []
        def fail(uid, row):
            calls.append(row)
            if len(calls) == 2:
                raise RuntimeError("simulated failure")
            return original(uid, row)
        with patch.object(self.store, "add", side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.send("Сохранить абонемент")
        self.assertEqual(self.store.passes(42), [])
        self.assertEqual(self.store.history(42), [])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM pass_deposits").fetchone()[0], 0)
        self.assertIsNotNone(self.store.draft(42))
        self.send("/cancel")
        self.assertIsNone(self.store.draft(42))

    def test_ai_shared_review_correction_and_no_automatic_save(self):
        self.ai()
        self.assertEqual(self.recognizer.ocr.call_count, 2)
        self.series("Верно", "2025", "Верно", "60", "Не помню",
                    "Верно", "Верно", "2", "Верно", "1250", "4")
        d = self.store.draft(42)
        self.assertEqual(d["step"], "pass_review")
        self.assertEqual([r["cost"] for r in d["sessions"]], [70000, 125000])
        self.assertEqual(self.store.history(42), [])
        self.send("Сохранить абонемент")
        self.assertEqual(len(self.store.history(42)), 2)
        self.assertIn("1250", self.store.passes(42)[0]["notes"])

    def test_ai_partial_failure_reuses_completed_ocr(self):
        self.recognizer.ocr.side_effect = [{"text": "front"}, RecognitionError("offline"), {"text": "back"}]
        self.send("/pass_ai")
        self.send(photo="front")
        self.send(photo="back")
        self.send("Распознать")
        self.bot.passes.process_job(self.job())
        self.assertEqual(self.store.draft(42)["step"], "pass_ready")
        self.assertEqual(len(self.store.draft(42)["ocr"]), 1)
        self.send("Распознать")
        self.bot.passes.process_job(self.job())
        self.assertEqual(self.recognizer.ocr.call_count, 3)
        self.assertEqual(self.store.draft(42)["step"], "pass_number")

    def test_ai_unconfigured_and_unauthorized_photos(self):
        bot = Tracker(self.store, 42)
        bot.passes.start(42, 42, ai=True)
        self.assertIsNone(self.store.draft(42))
        with self.assertLogs(level="WARNING"):
            self.send("/pass_ai", user=99)
            self.send(photo="foreign", user=99)
            self.send("/pass_ai", chat_type="group")
        self.recognizer.ocr.assert_not_called()
        self.send("/pass_ai")
        self.send(photo="front")
        self.send(photo="front")
        self.assertEqual(len(self.store.draft(42)["photos"]), 1)
        self.send("/cancel")
        self.assertIsNone(self.store.draft(42))

    def test_invalid_values_retain_draft_and_future_dates_rejected(self):
        self.series("/pass", "test", "9999")
        self.assertEqual(self.store.draft(42)["step"], "pass_year")
        self.series("2026", "-1")
        self.assertEqual(self.store.draft(42)["step"], "pass_deposits")
        self.series("0", "Не помню", "Без тренера", "20.09")
        self.assertEqual(self.store.draft(42)["step"], "pass_date")
        self.series("19.09", "0", "Не помню", "Сохранить абонемент")
        self.assertEqual(self.store.passes(42)[0]["used"], 0)

    def test_pending_job_survives_missing_key_after_restart(self):
        self.send("/pass_ai")
        self.send(photo="front")
        self.send(photo="back")
        self.send("Распознать")
        restored = Tracker(self.store, 42, lambda: date(2026, 9, 19))
        restored.passes.process_job(self.job())
        self.assertEqual(self.store.draft(42)["step"], "pass_ready")
        self.recognizer.ocr.assert_not_called()

    def test_pending_job_ignored_after_cancel(self):
        self.send("/pass_ai")
        self.send(photo="front")
        self.send(photo="back")
        self.send("Распознать")
        job = self.job()
        self.send("/cancel")
        self.bot.passes.process_job(job)
        self.recognizer.ocr.assert_not_called()

    def test_delete_updates_pass_balance(self):
        self.manual()
        self.send("Сохранить абонемент")
        row = self.store.history(42)[0]
        self.series("/delete " + str(row["id"]), "Удалить")
        self.assertEqual(self.store.passes(42)[0]["used"], 0)

    def test_large_preview_split_to_telegram_limits(self):
        self.manual()
        d = self.store.draft(42)
        d["sessions"] = [dict(d["sessions"][0], coach="Тренер " + "я" * 60) for _ in range(100)]
        with self.store.db:
            self.bot.passes.review(42, 42, d)
        for row in self.store.db.execute("SELECT payload FROM outbox WHERE kind='text'"):
            self.assertLessEqual(len(json.loads(row[0])["text"].encode("utf-16-le")), 8192)


class PassStorageAndNetworkTests(unittest.TestCase):
    def test_legacy_migration_preserves_ids_drafts_and_known_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "db.sqlite3"
            db = sqlite3.connect(path)
            db.executescript("""
                CREATE TABLE sessions(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,
                  activity_id TEXT NOT NULL,played_on TEXT NOT NULL,minutes INTEGER NOT NULL,
                  cost_kopecks INTEGER NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                  participants INTEGER,coach TEXT);
                INSERT INTO sessions VALUES(9,42,'tennis','2025-09-06',90,70000,'old',4,'Anna');
                INSERT INTO sessions VALUES(99,42,'tennis','2025-09-06',90,70000,'old',4,'Anna');
                DELETE FROM sessions WHERE id=99;
                CREATE TABLE drafts(user_id INTEGER PRIMARY KEY,data TEXT NOT NULL);
                INSERT INTO drafts VALUES(42,'{"step":"pass_year","flow":"pass"}');
            """)
            db.close()
            for _ in range(2):
                store = Store(path)
                try:
                    row = store.history(42)[0]
                    self.assertEqual((row["id"], row["minutes"], row["participants"], row["coach"]),
                                     (9, 90, 4, "Anna"))
                    self.assertEqual(store.draft(42)["step"], "pass_year")
                    self.assertEqual(store.db.execute("PRAGMA foreign_key_check").fetchall(), [])
                finally:
                    store.close()
            store = Store(path)
            try:
                with store.db:
                    new_id = store.add(42, {"date": "2025-09-07", "minutes": None, "cost": 0})
                self.assertGreater(new_id, 99)
            finally:
                store.close()

    def test_restart_mid_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "db.sqlite3"
            store = Store(path)
            tracker = Tracker(store, 42)
            for i, text in enumerate(["/pass", "465", "2025", "8000"], 1):
                tracker.handle({"update_id": i, "message": {"from": {"id": 42},
                    "chat": {"id": 42, "type": "private"}, "text": text}})
            store.close()
            store = Store(path)
            try:
                self.assertEqual(store.draft(42)["step"], "pass_minutes")
                self.assertEqual(store.draft(42)["deposits"], [800000])
            finally:
                store.close()

    def test_ocr_and_extraction_request_contract(self):
        recognizer = YandexRecognizer("fake-key", "folder")
        ocr = {"result": {"textAnnotation": {"fullText": "06.09 700", "blocks": [
            {"lines": [{"text": "700", "words": [{"text": "700",
              "boundingBox": {"vertices": [{"x": "50", "y": "100"}]}}]}]}]}}}
        extraction = {"result": {"alternatives": [{"status": "ALTERNATIVE_STATUS_FINAL",
            "message": {"text": json.dumps({"number": "465", "deposits": ["8000"],
                "sessions": [{"date": "06.09", "cost": "700"}], "notes": []})}}]}}
        with patch("pass_ai.urlopen", side_effect=[
                io.BytesIO(json.dumps(ocr).encode()), io.BytesIO(json.dumps(extraction).encode())]) as request:
            page = recognizer.ocr(b"jpeg")
            result = recognizer.extract([page, page])
        self.assertEqual(result["sessions"][0]["cost"], "700")
        first = json.loads(request.call_args_list[0].args[0].data)
        self.assertEqual(first["model"], "handwritten")
        second = json.loads(request.call_args_list[1].args[0].data)
        self.assertTrue(second["jsonObject"])
        self.assertIn("100", second["messages"][1]["text"])

    def test_malformed_results_and_network_errors_safe(self):
        from urllib.error import URLError
        for value in ([], {"sessions": "bad"}, {"sessions": [{}] * 101}, {"deposits": [8000]}):
            with self.assertRaises(RecognitionError):
                validate_result(value)
        recognizer = YandexRecognizer("secret", "folder")
        with patch("pass_ai.urlopen", side_effect=URLError("secret private request")):
            with self.assertRaises(RecognitionError) as error:
                recognizer.ocr(b"jpeg")
        self.assertNotIn("secret", str(error.exception))

    def test_telegram_photo_download_size_and_safe_error(self):
        from urllib.error import URLError
        telegram = Telegram("123:secret")
        with patch.object(telegram, "call", return_value={"file_path": "photos/file_1.jpg"}), \
                patch("bot.urlopen", return_value=io.BytesIO(b"jpeg")) as request:
            self.assertEqual(telegram.download_photo("id"), b"jpeg")
            self.assertEqual(request.call_args.args[0], "https://api.telegram.org/file/bot123:secret/photos/file_1.jpg")
        with patch.object(telegram, "call", return_value={"file_path": "../secret"}):
            with self.assertRaises(RecognitionError):
                telegram.download_photo("id")
        with patch.object(telegram, "call", return_value={"file_path": "photos/file.jpg"}), \
                patch("bot.urlopen", side_effect=URLError("123:secret")):
            with self.assertRaises(RecognitionError) as error:
                telegram.download_photo("id")
            self.assertNotIn("secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()
