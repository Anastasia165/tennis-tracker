import unittest
from datetime import date
from unittest.mock import patch
from bot import Tracker
from linking import snapshot
from store import Store


class LinkingTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.bot = Tracker(self.store, 42, lambda: date(2026, 9, 20))
        self.counter = 0
        with self.store.db:
            self.card = self.store.save_pass(42, {"token": "p1", "number": "465", "source": "manual",
                "deposits": [1000000], "sessions": []})
            self.a = self.store.add(42, {"date": "2026-09-19", "cost": 250000,
                                        "minutes": 90, "participants": 4, "coach": "Anna"})
            self.b = self.store.add(42, {"date": "2026-09-18", "cost": 100000,
                                        "minutes": None, "participants": None, "coach": None})

    def tearDown(self):
        self.store.close()

    def send(self, text):
        self.counter += 1
        update = {"update_id": self.counter, "message": {"from": {"id": 42},
                  "chat": {"id": 42, "type": "private"}, "text": text}}
        self.bot.handle(update)
        return update

    def series(self, *texts):
        for text in texts:
            self.send(text)

    def test_link_confirm_preserves_records_and_totals(self):
        before = [snapshot(r) for r in self.store.history(42)]
        self.series("Привязать тренировки", str(self.card), f"{self.a} {self.b}")
        self.assertIsNone(self.store.get_session(42, self.a)["pass_id"])
        self.assertEqual(self.store.draft(42)["step"], "link_confirm")
        update = self.send("Привязать")
        self.bot.handle(update)
        self.send("Привязать")
        self.assertEqual([snapshot(r) for r in self.store.history(42)], before)
        self.assertEqual(self.store.get_pass(42, self.card)["used"], 350000)
        self.assertEqual(self.store.get_session(42, self.a)["pass_id"], self.card)

    def test_cancel_restart_and_choose_again(self):
        self.series(f"/link {self.card}", str(self.a))
        self.bot = Tracker(self.store, 42)
        self.series("Выбрать другие", str(self.b), "/cancel")
        self.assertIsNone(self.store.get_session(42, self.b)["pass_id"])
        self.assertIsNone(self.store.draft(42))
        self.series(f"/link {self.card}", str(self.a), "Привязать")
        self.assertEqual(self.store.get_session(42, self.a)["pass_id"], self.card)

    def test_foreign_or_already_bound_sessions_and_cards_rejected(self):
        with self.store.db:
            foreign = self.store.add(99, {"date": "2026-09-19", "minutes": 60, "cost": 0})
            foreign_pass = self.store.save_pass(99, {"token": "foreign", "number": "x", "source": "manual",
                                                  "deposits": [0], "sessions": []})
        self.send(f"/link {foreign_pass}")
        self.assertIsNone(self.store.draft(42))
        self.series(f"/link {self.card}", str(foreign))
        self.assertEqual(self.store.draft(42)["step"], "link_select")
        with self.assertRaises(ValueError):
            self.store.link_sessions(42, foreign_pass, [snapshot(self.store.get_session(42, self.a))])
        self.series(str(self.a), "Привязать", f"/link {self.card}", str(self.a))
        self.assertEqual(self.store.draft(42)["step"], "link_select")

    def test_stale_selection_is_atomic(self):
        self.series(f"/link {self.card}", f"{self.a} {self.b}")
        with self.store.db:
            self.store.db.execute("UPDATE sessions SET cost_kopecks=999 WHERE id=?", (self.b,))
        self.send("Привязать")
        self.assertIsNone(self.store.get_session(42, self.a)["pass_id"])
        self.assertEqual(self.store.get_pass(42, self.card)["used"], 0)
        self.assertEqual(self.store.draft(42)["step"], "link_confirm")

    def test_duplicate_ids_deduplicated_and_pagination(self):
        with self.store.db:
            for _ in range(22):
                self.store.add(42, {"date": "2026-09-17", "minutes": 60, "cost": 0})
        self.series(f"/link {self.card}", "Дальше")
        self.assertEqual(self.store.draft(42)["page"], 1)
        self.series("Дальше", "Назад")
        self.assertEqual(self.store.draft(42)["page"], 0)
        self.series(f"{self.a} {self.a}", "Привязать")
        self.assertEqual(self.store.get_pass(42, self.card)["used"], 250000)

    def import_row(self):
        self.series("/pass", "new", "2026", "8000", "60", "Other",
                    "19.09", "700", "2", "Сохранить абонемент", "Использовать существующее", "1",
                    str(self.a))

    def test_import_reuses_after_explicit_confirmation_preserving_values(self):
        before = snapshot(self.store.get_session(42, self.a))
        self.import_row()
        self.assertEqual(self.store.draft(42)["step"], "pass_reuse_confirm")
        self.assertIsNone(self.store.get_session(42, self.a)["pass_id"])
        self.series("Да, использовать")
        self.assertEqual(self.store.draft(42)["sessions"][0]["cost"], 250000)
        self.series("Сохранить абонемент")
        self.assertEqual(len(self.store.history(42)), 2)
        self.assertEqual(snapshot(self.store.get_session(42, self.a)), before)
        self.assertNotEqual(self.store.get_session(42, self.a)["pass_id"], self.card)
        self.assertEqual(self.store.passes(42)[0]["used"], 250000)

    def test_remove_import_reference_does_not_delete_original(self):
        self.import_row()
        self.series("Да, использовать", "Убрать занятие", "1", "Сохранить абонемент")
        self.assertIsNone(self.store.get_session(42, self.a)["pass_id"])
        self.assertEqual(len(self.store.history(42)), 2)

    def test_import_stale_link_does_not_leave_partial_pass(self):
        self.import_row()
        self.send("Да, использовать")
        with self.store.db:
            self.store.link_sessions(42, self.card, [snapshot(self.store.get_session(42, self.a))])
        self.send("Сохранить абонемент")
        self.assertEqual(len(self.store.passes(42)), 1)
        self.assertIsNotNone(self.store.draft(42))

    def test_enqueue_failure_rolls_back_link_and_update(self):
        self.series(f"/link {self.card}", str(self.a))
        with patch.object(self.store, "enqueue", side_effect=RuntimeError("disk failure")):
            with self.assertRaises(RuntimeError):
                self.send("Привязать")
        self.assertIsNone(self.store.get_session(42, self.a)["pass_id"])
        self.assertFalse(self.store.processed(self.counter))
        self.assertEqual(self.store.draft(42)["step"], "link_confirm")

    def test_invalid_reuse_duplicate_reference_rejected_before_creation(self):
        row = self.store.get_session(42, self.a)
        entry = {"date": row["played_on"], "minutes": row["minutes"], "cost": row["cost_kopecks"],
                 "participants": row["participants"], "coach": row["coach"], "existing": snapshot(row)}
        draft = {"token": "invalid", "number": "bad", "source": "manual", "deposits": [0],
                 "sessions": [entry, entry]}
        with self.assertRaises(ValueError):
            self.store.save_pass(42, draft)
        self.assertEqual(len(self.store.passes(42)), 1)
        self.assertIsNone(self.store.get_session(42, self.a)["pass_id"])


if __name__ == "__main__":
    unittest.main()
