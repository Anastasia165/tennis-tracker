import unittest
from datetime import date
from bot import Tracker
from store import Store


class DuplicateDayTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.bot = Tracker(self.store, 42, lambda: date(2026, 9, 20))
        self.counter = 0
        with self.store.db:
            self.store.add(42, {"date": "2026-09-19", "minutes": 90, "cost": 250000})

    def tearDown(self):
        self.store.close()

    def send(self, text):
        self.counter += 1
        update = {"update_id": self.counter, "message": {"from": {"id": 42},
                  "chat": {"id": 42, "type": "private"}, "text": text}}
        self.bot.handle(update)
        return update

    def prepare(self, amount="2500", day="19.09.2026"):
        for text in (f"/add {day} 60 {amount}", "2", "Анна", "Сохранить"):
            self.send(text)

    def test_same_day_requires_separate_confirmation_even_different_price(self):
        self.prepare("1000")
        self.assertEqual(len(self.store.history(42)), 1)
        self.assertEqual(self.store.draft(42)["step"], "duplicate_confirm")
        self.send("Сохранить")
        self.assertEqual(len(self.store.history(42)), 1)
        self.send("Нет, не добавлять")
        self.assertEqual(len(self.store.history(42)), 1)
        self.assertIsNone(self.store.draft(42))

    def test_confirm_after_restart_and_repeated_update(self):
        self.prepare()
        self.bot = Tracker(self.store, 42, lambda: date(2026, 9, 20))
        update = self.send("Да, это другое занятие")
        self.bot.handle(update)
        self.send("Да, это другое занятие")
        self.assertEqual(len(self.store.history(42)), 2)

    def test_other_day_and_other_owner_do_not_trigger(self):
        with self.store.db:
            self.store.add(99, {"date": "2026-09-18", "minutes": 60, "cost": 100})
        self.prepare(day="18.09.2026")
        self.assertEqual(len(self.store.history(42)), 2)
        self.assertIsNone(self.store.draft(42))

    def test_guided_input_checks_same_day(self):
        for text in ("/add", "Вчера", "90", "1500", "3", "Без тренера", "Сохранить"):
            self.send(text)
        self.assertEqual(self.store.draft(42)["step"], "duplicate_confirm")
        self.send("/cancel")
        self.assertEqual(len(self.store.history(42)), 1)

    def test_pass_checks_existing_date_and_internal_dates_with_different_cost(self):
        draft = {"number": "new", "sessions": [
            {"date": "2026-09-19", "cost": 100},
            {"date": "2026-09-18", "cost": 200},
            {"date": "2026-09-18", "cost": 300}]}
        conflicts = self.store.pass_conflicts(42, draft)
        self.assertEqual(len(conflicts), 2)
        self.assertIn("строка 1", conflicts[0])
        self.assertIn("строка 3", conflicts[1])

    def test_pass_wizard_different_cost_requires_confirmation(self):
        for text in ("/pass", "new", "2026", "1000", "60", "Анна",
                     "19.09", "1000", "2", "Сохранить абонемент"):
            self.send(text)
        self.assertEqual(self.store.passes(42), [])
        self.assertEqual(self.store.draft(42)["step"], "pass_duplicates")
        self.send("Это разные занятия")
        self.assertEqual(len(self.store.passes(42)), 1)
        self.assertEqual(len(self.store.history(42)), 2)


if __name__ == "__main__":
    unittest.main()
