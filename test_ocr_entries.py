import json
import unittest
from datetime import date
from unittest.mock import Mock
from bot import Tracker
from pass_ai import normalize_amount, column_amounts, fill_missing_amounts, validate_result
from store import Store


def word(text, x, y, width=45):
    return {"text": text, "box": [{"x": x, "y": y}, {"x": x + width, "y": y + 20}]}


def table_page():
    return {"text": "Дата 06.09 13.09\nЧасы\nСумма 700 840", "words": [
        word("Дата", 0, 0), word("06.09", 100, 0), word("13.09", 200, 0),
        word("Часы", 0, 45),
        word("Сумма", 0, 90), word("700", 105, 90), word("840", 205, 90),
        word("1250", 205, 140)]}


class AmountRecognitionTests(unittest.TestCase):
    def test_normalization_and_zero(self):
        for raw, expected in [(700, "700"), (0, "0"), ("1 400", "1400"),
                              ("1\u00a0250,50 ₽", "1250.50"), ("840 руб.", "840")]:
            self.assertEqual(normalize_amount(raw), expected)
        for raw in (None, True, "700 840 60", "1O50", "NaN", -1, "1.999"):
            self.assertIsNone(normalize_amount(raw))
        rows = validate_result({"sessions": [{"date": "06.09", "cost": 700},
                                             {"date": "13.09", "cost": 0}]})["sessions"]
        self.assertEqual([r["cost"] for r in rows], ["700", "0"])

    def test_geometry_matches_columns_not_flat_text_order(self):
        self.assertEqual(column_amounts(table_page()),
                         [{"date": "06.09", "cost": "700"}, {"date": "13.09", "cost": "840"}])
        result = {"sessions": [{"date": "06.09", "cost": None}, {"date": "13.09", "cost": "1250"}]}
        fill_missing_amounts(result, [{"text": "front"}, table_page()])
        self.assertEqual([r["cost"] for r in result["sessions"]], ["700", "1250"])

    def test_ambiguous_boxes_and_unlabelled_numbers_not_used(self):
        page = table_page()
        page["words"].append(word("710", 100, 90))
        self.assertNotIn("06.09", [r["date"] for r in column_amounts(page)])
        self.assertEqual(column_amounts({"words": [word("06.09", 100, 0), word("700", 100, 90)]}), [])


class PerSessionParticipantsTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.recognizer = Mock()
        self.recognizer.ocr.return_value = {"text": "Дата 06.09; Сумма 700", "words": []}
        self.recognizer.extract.return_value = {"number": "1", "deposits": ["2000"],
            "notes": [], "sessions": [{"date": "06.09", "cost": "1 400 ₽"}, {"date": "13.09", "cost": 0}]}
        self.telegram = Mock()
        self.telegram.download_photo.return_value = b"fake jpeg"
        self.bot = Tracker(self.store, 42, lambda: date(2026, 9, 20),
                           recognizer=self.recognizer, telegram=self.telegram)
        self.index = 0

    def tearDown(self):
        self.store.close()

    def send(self, text, photo=None):
        self.index += 1
        message = {"from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": text}
        if photo:
            message["photo"] = [{"file_id": photo}]
        self.bot.handle({"update_id": self.index, "message": message})

    def series(self, *texts):
        for text in texts:
            self.send(text)

    def test_manual_asks_after_each_cost_and_does_not_inherit_people(self):
        self.series("/pass", "1", "2026", "2000", "60")
        self.assertEqual(self.store.draft(42)["step"], "pass_coach")
        self.series("Anna", "06.09", "700")
        self.assertEqual(self.store.draft(42)["step"], "pass_entry_people")
        self.send("0")
        self.assertEqual(self.store.draft(42)["step"], "pass_entry_people")
        self.series("2", "Добавить занятие", "13.09", "840")
        self.assertIsNone(self.store.draft(42)["current"]["participants"])
        self.series("5", "Сохранить абонемент")
        self.assertEqual([r["participants"] for r in self.store.month(42, "2026-09")], [2, 5])

    def test_ai_amount_buttons_and_terminal_text(self):
        self.send("/pass_ai")
        self.send("", "front")
        self.send("", "back")
        self.send("Распознать")
        row = self.store.db.execute("SELECT * FROM outbox WHERE kind='pass_ai'").fetchone()
        job = {**dict(row), "payload": json.loads(row["payload"])}
        with self.assertLogs(level="INFO") as captured:
            self.bot.passes.process_job(job)
        log = "\n".join(captured.output)
        self.assertIn("лицевая", log)
        self.assertIn("оборот", log)
        self.assertIn("Дата 06.09; Сумма 700", log)
        self.series("Верно", "2026", "Верно", "60", "Anna", "Верно")
        self.assertEqual(self.store.draft(42)["candidate"]["cost"], "1400")
        self.series("Верно", "2", "Верно")
        payload = json.loads(self.store.db.execute("SELECT payload FROM outbox ORDER BY id DESC LIMIT 1").fetchone()[0])
        self.assertIn(["Верно"], payload["keyboard"])
        self.assertIn("0,00", payload["text"])
        self.series("Верно", "Не помню", "Сохранить абонемент")
        rows = self.store.month(42, "2026-09")
        self.assertEqual([r["cost_kopecks"] for r in rows], [140000, 0])
        self.assertEqual([r["participants"] for r in rows], [2, None])

    def test_old_default_people_are_not_reused(self):
        with self.store.db:
            self.store.set_draft(42, {"flow": "pass", "step": "pass_date", "year": 2026,
                "defaults": {"participants": 8, "minutes": 60, "coach": "Anna"}, "sessions": []})
        self.series("06.09", "700")
        self.assertEqual(self.store.draft(42)["step"], "pass_entry_people")
        self.assertIsNone(self.store.draft(42)["current"]["participants"])


if __name__ == "__main__":
    unittest.main()
