"""End-to-end menu tests use synthetic in-memory databases; never load .env."""
import json
import sqlite3
import tempfile
import unittest
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from bot import Tracker, Telegram
from navigation import HOME
from reports import overview_chart
from store import Store


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.bot = Tracker(self.store, 42, lambda: date(2026, 9, 21))
        self.counter = 0

    def tearDown(self):
        self.store.close()

    def send(self, *texts, user=42):
        for text in texts:
            self.counter += 1
            update = {"update_id": self.counter, "message": {
                "from": {"id": user}, "chat": {"id": user, "type": "private"}, "text": text}}
            self.bot.handle(update)
        return update

    def last(self):
        r = self.store.db.execute("SELECT payload FROM outbox WHERE kind='text' ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(r[0])

    def select(self, fragment):
        labels = [v for row in self.last()["keyboard"] for v in row]
        choices = [v for v in labels if fragment in v]
        self.assertEqual(len(choices), 1, (fragment, labels))
        self.send(choices[0])

    def rows(self):
        return self.store.records(42, "2000-01-01", "2026-09-21")

    def make_pass(self, uid=42, amount=800000):
        with self.store.db:
            return self.store.save_pass(uid, dict(number="Клубная карта", source="manual",
                token=uuid.uuid4().hex, deposits=[amount], sessions=[]))

    def start_training(self, day="Сегодня", payment="Отдельная оплата", amount="2500"):
        self.send("/start", "➕ Добавить запись", "🏫 Тренировка с тренером",
                  day, "Клуб", "Анна", "90", "4", amount)
        self.send(payment, "Пропустить")

    def make_training(self, day="Сегодня", payment="Отдельная оплата", amount="2500"):
        self.start_training(day, payment, amount)
        self.send("Сохранить")
        return self.rows()[0]

    def make_game(self):
        self.send("/menu", "➕ Добавить запись", "🎾 Игра без тренера", "Вчера",
                  "Парк", "60", "Парная", "4", "Оля, Ира, Маша", "6:4", "Победа", "1200", "Пропустить", "Сохранить")
        return self.rows()[0]

    def make_tournament(self):
        self.send("/menu", "➕ Добавить запись", "🏆 Турнир", "Новый турнир",
                  "Осенний", "19.09.2026", "Клуб", "Одиночная", "3000", "3", "Пропустить", "Сохранить")
        row = self.store.tournaments_in(42, "2000-01-01", "2026-09-21")
        self.assertEqual(len(row), 1, self.last())
        return row[0]

    def match(self, result="Победа", duplicate=False):
        self.send("Добавить матч", "Оставить", "Оставить", "60", "Оставить",
                  "2", "Оля", "6:4", result, "Пропустить", "Сохранить")
        if duplicate:
            self.send("Да, это другое занятие")

    def test_home_compact_and_both_import_methods_reachable(self):
        self.send("/start")
        self.assertEqual(self.last()["keyboard"], HOME)
        self.send("➕ Добавить запись", "💳 Абонемент / пополнение", "Новый абонемент")
        self.select("Вручную")
        self.assertEqual(self.store.draft(42)["flow"], "pass")
        self.send("/cancel", "➕ Добавить запись", "💳 Абонемент / пополнение", "Новый абонемент")
        with patch.object(self.bot.passes, "start") as start:
            self.select("По фото")
            start.assert_called_once_with(42, 42, True)

    def test_training_confirmation_payment_and_no_cash_double_count(self):
        pid = self.make_pass()
        self.start_training(payment=f"Абонемент №{pid} · Клубная карта")
        self.assertEqual(self.rows(), [])
        self.assertIn("5 500,00", self.last()["text"])
        update = self.send("Сохранить")
        self.bot.handle(update)
        self.send("Сохранить")  # Stale confirm must not create another record.
        row = self.rows()[0]
        self.assertEqual((row["kind"], row["pass_id"], row["club"], row["participants"]), ("training", pid, "Клуб", 4))
        totals = self.store.finances(42, "2026-09-01", "2026-09-21")
        self.assertEqual((totals["paid"], totals["consumed"], totals["balance"], totals["unknown"]),
                         (0, 250000, 550000, 800000))
        self.assertEqual(len(self.rows()), 1)

    def test_deposit_date_is_confirmed_and_counted_once(self):
        pid = self.make_pass()
        self.send("/passes")
        self.select(f"Абонемент №{pid}")
        self.send("Пополнить", "Сегодня", "5000")
        self.assertEqual(self.store.get_pass(42, pid)["paid"], 800000)
        self.send("Сохранить", "Сохранить")
        self.assertEqual(self.store.get_pass(42, pid)["paid"], 1300000)
        f = self.store.finances(42, "2026-09-01", "2026-09-21")
        self.assertEqual((f["paid"], f["unknown_count"]), (500000, 1))
        self.send("Изменить", "Дата", "01.08.2026", "Сохранить")
        self.assertEqual(self.store.finances(42, "2026-09-01", "2026-09-21")["paid"], 0)
        self.assertEqual(self.store.finances(42, "2026-08-01", "2026-08-31")["paid"], 500000)

    def test_date_old_deposit_without_changing_balance(self):
        pid = self.make_pass()
        self.send("/passes")
        self.select(f"Абонемент №{pid}")
        self.send("Пополнения и их даты")
        self.select("Дата неизвестна")
        self.send("Изменить", "Дата", "15.09.2026", "Сохранить")
        f = self.store.finances(42, "2026-09-01", "2026-09-21")
        self.assertEqual((f["paid"], f["balance"], f["unknown_count"]), (800000, 800000, 0))

    def test_game_results_and_unknown_time(self):
        game = self.make_game()
        self.assertEqual((game["kind"], game["format"], game["result"], game["cost_kopecks"]),
                         ("game", "Парная", "Победа", 120000))
        self.assertIsNone(game["pass_id"])
        self.assertEqual(self.store.unlinked(42), [])  # Games cannot accidentally be charged to a club card.
        self.send("Изменить", "Длительность", "Не помню", "Сохранить")
        self.assertIsNone(self.rows()[0]["minutes"])
        self.send("🎾 Мой теннис", "🎾 Игры")
        self.assertIn("Победа", self.last()["text"])
        self.assertIn("Оля", self.last()["text"])
        self.assertIn("из 1", self.last()["text"])

    def test_tournament_fee_once_multiple_matches_and_cascade(self):
        t = self.make_tournament()
        self.match()
        row = self.rows()[0]
        self.assertEqual((row["kind"], row["tournament_id"], row["cost_kopecks"]), ("match", t["id"], 0))
        self.bot.navigation.card(42, 42, "tournament", t["id"])
        self.match(result="Поражение", duplicate=True)
        matches = self.store.matches(42, t["id"])
        self.assertEqual(len(matches), 2, self.last())
        self.assertEqual(self.store.finances(42, "2026-09-01", "2026-09-21")["paid"], 300000)
        self.bot.navigation.card(42, 42, "tournament", t["id"])
        self.assertIn("побед: 1", self.last()["text"])
        self.send("Матчи турнира")
        self.assertEqual(len(self.store.draft(42)["choices"]), 2)
        self.send("⬅️ Назад", "Удалить")
        self.assertIsNotNone(self.store.tournament(42, t["id"]))
        self.assertIn("матчи", self.last()["text"])
        self.send("Да, удалить")
        self.assertEqual(self.store.matches(42, t["id"]), [])
        self.assertEqual(self.store.finances(42, "2026-09-01", "2026-09-21")["paid"], 0)

    def test_expense_link_edit_and_delete_confirmation(self):
        game = self.make_game()
        self.send("/menu", "➕ Добавить запись", "🧾 Другой расход", "Сегодня",
                  "Струны и перетяжка", "2000", f"занятие {game['id']}", "Перед игрой", "Сохранить")
        r = dict(self.store.db.execute("SELECT * FROM expenses").fetchone())
        self.assertEqual((r["session_id"], r["amount"]), (game["id"], 200000))
        self.send("Изменить", "Сумма", "2500", "Сохранить")
        self.assertEqual(self.store.expense(42, r["id"])["amount"], 250000)
        self.send("Удалить", "⬅️ Назад")
        self.assertIsNotNone(self.store.expense(42, r["id"]))
        self.send("Удалить", "Да, удалить")
        self.assertIsNone(self.store.expense(42, r["id"]))

    def test_duplicate_session_change_date_and_cancel(self):
        self.make_training()
        self.start_training()
        self.send("Сохранить")
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.store.draft(42)["mode"], "duplicate")
        self.send("Изменить", "Дата", "Вчера", "Сохранить")
        self.assertEqual(len(self.rows()), 2)
        self.start_training()
        self.send("Сохранить", "Нет, не добавлять")
        self.assertEqual(len(self.rows()), 2)

    def test_edit_preserves_id_and_refunds_old_pass(self):
        pid = self.make_pass()
        r = self.make_training(payment=f"Абонемент №{pid} · Клубная карта")
        self.send("Изменить", "Участники", "2", "Сохранить")
        self.assertEqual(self.rows()[0]["id"], r["id"])
        self.assertEqual(self.rows()[0]["participants"], 2)
        self.assertEqual(self.store.get_pass(42, pid)["used"], 250000)
        self.send("Изменить", "Оплата", "Отдельная оплата", "Сохранить")
        self.assertEqual(self.store.get_pass(42, pid)["used"], 0)
        self.assertEqual(self.store.finances(42, "2026-09-01", "2026-09-21")["paid"], 250000)

    def test_stale_edit_does_not_overwrite_and_owner_isolation(self):
        r = self.make_training()
        self.send("Изменить", "Сумма", "1000")
        with self.store.db:
            self.store.db.execute("UPDATE sessions SET cost_kopecks=180000 WHERE id=?", (r["id"],))
        self.send("Сохранить")
        self.assertEqual(self.rows()[0]["cost_kopecks"], 180000)
        self.assertIn("изменилась", self.last()["text"])
        self.assertEqual(self.store.records(99, "2000-01-01", "2026-09-21"), [])
        self.assertEqual(self.store.finances(99, "2000-01-01", "2026-09-21")["paid"], 0)
        with self.assertRaises(ValueError):
            self.bot.navigation.card(99, 99, "session", r["id"])
        with self.assertLogs(level="WARNING"):
            self.send("➕ Добавить запись", user=99)
        self.assertIsNone(self.store.draft(99))

    def test_periods_separate_and_custom_calendar(self):
        self.make_training("01.08.2026")
        self.make_training("20.09.2026")
        self.send("🎾 Мой теннис")
        self.assertIn("Тренировок: 1", self.last()["text"])
        self.send("🗓 Выбрать период", "Прошлый месяц")
        self.assertIn("01.08.2026", self.last()["text"])
        self.assertIn("Тренировок: 1", self.last()["text"])
        self.send("💳 Финансы")
        self.assertIn("01.09.2026", self.last()["text"])
        self.send("🗓 Выбрать период", "Другой период", "01.08.2026", "21.09.2026")
        self.assertIn("5 000,00", self.last()["text"])
        self.assertEqual(self.store.preferences(42)["sport_period"], "previous")
        self.send("🎾 Мой теннис", "🗓 Выбрать период", "Другой период", "Календарь")
        draft = self.store.draft(42)
        self.callback("2026-08-01", draft["calendar_token"])
        self.send("Календарь")
        new_token = self.store.draft(42)["calendar_token"]
        self.assertNotEqual(draft["calendar_token"], new_token)
        self.callback("2026-08-20", draft["calendar_token"])
        self.assertEqual(self.store.draft(42)["mode"], "period_end")
        self.callback("2026-09-21", new_token)
        self.assertIn("Тренировок: 2", self.last()["text"])

    def callback(self, value, token):
        self.counter += 1
        self.bot.handle({"update_id": self.counter, "callback_query": {
            "id": str(self.counter), "from": {"id": 42},
            "message": {"message_id": 123, "chat": {"id": 42, "type": "private"}},
            "data": f"cal:{token}:day:{value}"}})

    def test_calendar_record_and_back_navigation(self):
        self.send("/menu", "➕ Добавить запись", "🏫 Тренировка с тренером", "Календарь")
        token = self.store.draft(42)["calendar_token"]
        self.callback("2026-08-31", token)
        self.assertEqual(self.store.draft(42)["data"]["date"], "2026-08-31")
        self.send("⬅️ Назад")
        self.assertEqual(self.store.draft(42)["step"], "date")
        self.send("Отменить ввод")
        self.assertEqual(self.last()["keyboard"], HOME)

    def test_settings_defaults_and_participants_never_shared(self):
        self.send("/settings", "🏫 Мои клубы", "Добавить", "Лужники",
                  "👤 Мои тренеры", "Добавить", "Мария",
                  "⏱ Обычная длительность", "90")
        self.send("/menu", "➕ Добавить запись", "🏫 Тренировка с тренером", "Сегодня")
        self.assertIn("Лужники", self.last()["text"])
        self.send("Оставить")
        self.assertIn("Мария", self.last()["text"])
        self.send("Оставить")
        self.assertIn("90", self.last()["text"])
        self.send("Оставить")
        self.assertNotIn(["Оставить"], self.last()["keyboard"])
        self.send("2", "1200", "Отдельная оплата", "Пропустить", "Сохранить")
        self.assertEqual(self.rows()[0]["participants"], 2)
        self.send("/settings", "🏫 Мои клубы", "Лужники", "Переименовать", "Лужники 2")
        self.assertEqual(self.store.preferences(42)["club"], "Лужники 2")
        self.assertEqual(self.rows()[0]["club"], "Лужники")

    def test_timezone_setting_affects_today_and_validates(self):
        self.send("/settings", "🌍 Часовой пояс", "99")
        self.assertNotIn("utc_offset", self.store.preferences(42))
        self.send("-5")
        with patch("bot.datetime") as clock:
            clock.now.return_value = datetime(2026, 9, 20, 22, tzinfo=timezone.utc)
            self.assertEqual(self.bot.today(), date(2026, 9, 20))
            self.assertEqual(clock.now.call_args[0][0].utcoffset(None).total_seconds(), -18000)

    def test_list_pagination_filter_and_pass_club(self):
        pid = self.make_pass()
        with self.store.db:
            for n in range(23):
                self.store.add(42, dict(date=f"2026-09-{n % 20 + 1:02d}", minutes=60, cost=100))
        self.send("/menu", "🎾 Мой теннис", "📅 История")
        self.assertIn("Всего: 23", self.last()["text"])
        self.send("Следующие ▶️")
        self.assertIn("страница 2", self.last()["text"])
        self.send("Следующие ▶️")
        self.assertIn("страница 3", self.last()["text"])
        self.send("/passes")
        self.select(f"Абонемент №{pid}")
        self.send("Указать клуб", "Центр", "Занятия по абонементу")
        self.assertIn("пока нет", self.last()["text"])
        self.send("⬅️ Назад")
        self.assertIn("Центр", self.last()["text"])
        self.send("💳 Финансы", "🧾 История операций", "Только списания")
        self.assertIn("пока нет", self.last()["text"])

    def test_delete_session_keeps_separate_expense(self):
        game = self.make_game()
        with self.store.db:
            eid = self.store.save_record(42, dict(entity="expense", token="expense-token", data={
                "date": "2026-09-20", "cost": 10000, "category": "Инвентарь", "session_id": game["id"]}))
        self.send("Удалить", "Да, удалить")
        self.assertIsNotNone(self.store.expense(42, eid))
        self.assertIsNone(self.store.expense(42, eid)["session_id"])
        self.assertEqual(self.store.finances(42, "2026-09-01", "2026-09-21")["paid"], 10000)

    def test_finance_categories_and_sports_charts_generate_png_without_network(self):
        self.make_training()
        self.make_game()
        self.make_tournament()
        self.send("💳 Финансы", "📊 Расходы по категориям")
        p = json.loads(self.store.db.execute("SELECT payload FROM outbox WHERE kind='overview_chart' ORDER BY id DESC LIMIT 1").fetchone()[0])
        self.assertEqual(sum(p["values"].values()), 670000)
        image = overview_chart(p)
        self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))
        self.send("🎾 Мой теннис", "📊 Динамика", "По неделям")
        p = json.loads(self.store.db.execute("SELECT payload FROM outbox WHERE kind='overview_chart' ORDER BY id DESC LIMIT 1").fetchone()[0])
        self.assertTrue(overview_chart(p).startswith(b"\x89PNG"))
        p["group"] = "month"
        self.assertTrue(overview_chart(p).startswith(b"\x89PNG"))
        api = Telegram("1:FAKE")
        with patch.object(api, "call") as call:
            api.deliver(dict(kind="overview_chart", chat_id=42, payload=p))
            self.assertEqual(call.call_args[0][0], "sendPhoto")

    def test_invalid_answers_keep_keyboard_and_no_early_writes(self):
        self.send("/menu", "➕ Добавить запись", "🏫 Тренировка с тренером", "Сегодня", "Клуб", "Анна", "90", "0")
        self.assertEqual(self.store.draft(42)["fields"][self.store.draft(42)["index"]], "participants")
        self.assertIn(["1", "2", "3"], self.last()["keyboard"])
        self.send("4", "-2")
        self.assertEqual(self.rows(), [])
        self.send("2000", "Несуществующая карта")
        self.assertEqual(self.rows(), [])
        self.assertIn(["Отдельная оплата"], self.last()["keyboard"])

    def test_no_nontraining_link_or_foreign_expense(self):
        game = self.make_game()
        from linking import snapshot
        pid = self.make_pass()
        with self.assertRaises(ValueError):
            self.store.link_sessions(42, pid, [snapshot(game)])
        with self.assertRaises(ValueError):
            self.store.save_record(99, dict(entity="expense", token="bad-owner", data={
                "date": "2026-09-20", "cost": 100, "category": "Инвентарь", "session_id": game["id"]}))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM expenses").fetchone()[0], 0)

    def test_reopen_migration_preserves_records_and_unknown_deposit_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic.sqlite3"
            db = Store(path)
            with db.db:
                pid = db.save_pass(42, dict(number="old", source="manual", token="old-pass",
                                           deposits=[800000], sessions=[]))
                sid = db.add(42, dict(date="2026-08-01", minutes=None, cost=125000, participants=3, coach="Анна"))
                db.db.execute("UPDATE sessions SET pass_id=? WHERE id=?", (pid, sid))
            before = db.get_session(42, sid)
            db.close()
            db = Store(path)
            self.assertEqual(db.get_session(42, sid), before)
            self.assertEqual(db.get_pass(42, pid)["used"], 125000)
            self.assertIsNone(db.pass_deposits(42, pid)[0]["paid_on"])
            self.assertEqual(db.db.execute("PRAGMA foreign_key_check").fetchall(), [])
            db.close()

    def test_old_unfinished_draft_survives_restart(self):
        with self.store.db:
            self.store.set_draft(42, dict(step="confirm", date="2026-09-10", minutes=90, cost=100000))
        self.bot = Tracker(self.store, 42, lambda: date(2026, 9, 21))
        self.send("Сохранить")
        self.assertEqual(self.rows()[0]["kind"], "training")

    def test_migration_from_previous_schema_retains_ids_balances_and_drafts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "previous.sqlite3"
            old = sqlite3.connect(path)
            old.executescript("""
                CREATE TABLE activities (id TEXT PRIMARY KEY,title TEXT NOT NULL);
                INSERT INTO activities VALUES ('tennis','Tennis');
                CREATE TABLE passes (id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,
                    number TEXT NOT NULL,source TEXT NOT NULL,import_token TEXT UNIQUE NOT NULL,
                    notes TEXT NOT NULL DEFAULT '[]',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                INSERT INTO passes(id,user_id,number,source,import_token) VALUES(15,42,'old','manual','old-token');
                CREATE TABLE pass_deposits (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pass_id INTEGER NOT NULL REFERENCES passes(id),amount INTEGER NOT NULL);
                INSERT INTO pass_deposits VALUES(17,15,800000);
                CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,
                    activity_id TEXT NOT NULL REFERENCES activities(id),played_on TEXT NOT NULL,minutes INTEGER,
                    cost_kopecks INTEGER NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    participants INTEGER,coach TEXT,pass_id INTEGER REFERENCES passes(id));
                INSERT INTO sessions(id,user_id,activity_id,played_on,minutes,cost_kopecks,participants,coach,pass_id)
                    VALUES(22,42,'tennis','2026-09-01',NULL,125000,4,'Anna',15);
                CREATE TABLE drafts(user_id INTEGER PRIMARY KEY,data TEXT NOT NULL);
                INSERT INTO drafts VALUES(42,'{"step":"date"}');
            """)
            old.close()
            db = Store(path)
            self.assertEqual(db.get_session(42, 22)["kind"], "training")
            self.assertIsNone(db.get_session(42, 22)["minutes"])
            self.assertEqual(db.draft(42), {"step": "date"})
            self.assertEqual(db.get_pass(42, 15)["paid"] - db.get_pass(42, 15)["used"], 675000)
            self.assertIsNone(db.deposit(42, 17)["paid_on"])
            with db.db:
                sid = db.add(42, dict(date="2026-09-21", minutes=60, cost=100000))
            self.assertGreater(sid, 22)
            db.close()

    def test_new_form_can_resume_after_tracker_restart(self):
        self.send("/menu", "➕ Добавить запись", "🎾 Игра без тренера",
                  "Сегодня", "Парк", "90", "Одиночная")
        saved = self.store.draft(42)
        self.bot = Tracker(self.store, 42, lambda: date(2026, 9, 21))
        self.assertEqual(self.store.draft(42), saved)
        self.send("2", "Оля", "Пропустить", "Не указан", "900", "Пропустить", "Сохранить")
        self.assertEqual(self.rows()[0]["kind"], "game")
        self.assertIsNone(self.rows()[0]["score"])

    def test_last_values_suggested_but_no_participant_default(self):
        self.make_training()
        self.send("/menu", "➕ Добавить запись", "🏫 Тренировка с тренером", "Сегодня")
        self.assertIn("Предложено: Клуб", self.last()["text"])
        self.send("Оставить")
        self.assertIn("Предложено: Анна", self.last()["text"])
        self.send("Оставить", "Оставить")
        self.assertNotIn(["Оставить"], self.last()["keyboard"])
        self.assertIsNone(self.store.draft(42)["data"]["participants"])

    def test_payment_atomic_rollback_on_failed_update(self):
        with self.store.db:
            self.store.db.execute("""CREATE TRIGGER fail_session_update BEFORE UPDATE OF kind ON sessions
                BEGIN SELECT RAISE(ABORT,'synthetic failure'); END""")
        draft = dict(entity="session", token="failing-session", data=dict(
            kind="training", date="2026-09-21", minutes=60, cost=10000))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save_record(42, draft)
        self.assertEqual(self.rows(), [])

    def test_foreign_pass_and_tournament_rejected_before_session_insert(self):
        pid = self.make_pass(uid=99)
        for extra in (dict(pass_id=pid), dict(kind="match", tournament_id=999, cost=0)):
            draft = dict(entity="session", token="foreign", data={
                "kind": "training", "date": "2026-09-21", "minutes": 60, "cost": 10000, **extra})
            with self.assertRaises(ValueError):
                self.store.save_record(42, draft)
        self.assertEqual(self.rows(), [])



if __name__ == "__main__":
    unittest.main()
