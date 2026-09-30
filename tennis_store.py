"""Additional tennis records and cash accounting. No environment or network access."""
import json
from collections import defaultdict


class TennisStore:
    def migrate_tennis(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS preferences (
                user_id INTEGER PRIMARY KEY, data TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS tournaments (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                name TEXT NOT NULL, played_on TEXT NOT NULL, club TEXT,
                format TEXT, fee INTEGER NOT NULL CHECK(fee>=0),
                place TEXT, note TEXT, token TEXT UNIQUE NOT NULL
            );
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                played_on TEXT NOT NULL, category TEXT NOT NULL,
                amount INTEGER NOT NULL CHECK(amount>=0), note TEXT,
                session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
                tournament_id INTEGER REFERENCES tournaments(id) ON DELETE SET NULL,
                token TEXT UNIQUE NOT NULL
            );
        """)
        with self.db:
            additions = {
                "sessions": {
                    "kind": "TEXT NOT NULL DEFAULT 'training'",
                    "club": "TEXT", "format": "TEXT", "opponent": "TEXT",
                    "score": "TEXT", "result": "TEXT", "note": "TEXT",
                    "tournament_id": "INTEGER REFERENCES tournaments(id) ON DELETE CASCADE",
                },
                "passes": {"club": "TEXT"},
                "pass_deposits": {"paid_on": "TEXT", "token": "TEXT"},
            }
            for table, columns in additions.items():
                current = {r[1] for r in self.db.execute("PRAGMA table_info(" + table + ")")}
                for name, definition in columns.items():
                    if name not in current:
                        self.db.execute("ALTER TABLE " + table + " ADD COLUMN " + name + " " + definition)
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS deposit_token ON pass_deposits(token)")
            self.db.execute("CREATE INDEX IF NOT EXISTS tournament_owner ON tournaments(user_id,played_on)")
            self.db.execute("CREATE INDEX IF NOT EXISTS expense_owner ON expenses(user_id,played_on)")

    def preferences(self, uid):
        row = self.db.execute("SELECT data FROM preferences WHERE user_id=?", (uid,)).fetchone()
        return json.loads(row[0]) if row else {}

    def set_preferences(self, uid, values):
        self.db.execute("INSERT OR REPLACE INTO preferences VALUES (?,?)",
                        (uid, json.dumps(values, ensure_ascii=False)))

    def records(self, uid, start, end, kind=None):
        sql = "SELECT * FROM sessions WHERE user_id=? AND activity_id='tennis' AND played_on BETWEEN ? AND ?"
        args = [uid, start, end]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        return [dict(r) for r in self.db.execute(sql + " ORDER BY played_on DESC,id DESC", args)]

    def tournaments_in(self, uid, start, end):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM tournaments WHERE user_id=? AND played_on BETWEEN ? AND ? ORDER BY played_on DESC,id DESC",
            (uid, start, end))]

    def tournament(self, uid, record_id):
        r = self.db.execute("SELECT * FROM tournaments WHERE id=? AND user_id=?", (record_id, uid)).fetchone()
        return dict(r) if r else None

    def matches(self, uid, tournament_id):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM sessions WHERE user_id=? AND tournament_id=? ORDER BY played_on,id",
            (uid, tournament_id))]

    def expense(self, uid, record_id):
        r = self.db.execute("SELECT * FROM expenses WHERE id=? AND user_id=?", (record_id, uid)).fetchone()
        return dict(r) if r else None

    def deposit(self, uid, record_id):
        r = self.db.execute("""SELECT d.* FROM pass_deposits d JOIN passes p ON p.id=d.pass_id
            WHERE d.id=? AND p.user_id=?""", (record_id, uid)).fetchone()
        return dict(r) if r else None

    def owned_record(self, uid, entity, record_id):
        readers = {"session": self.get_session, "tournament": self.tournament,
                   "expense": self.expense, "deposit": self.deposit, "pass": self.get_pass}
        return readers[entity](uid, record_id)

    def save_record(self, uid, draft):
        """Validated UI values; compare snapshots and save every related change atomically."""
        entity, data = draft["entity"], draft["data"]
        record_id = draft.get("id")
        with self.pass_transaction():
            if record_id:
                old = self.owned_record(uid, entity, record_id)
                if not old or old != draft.get("original"):
                    raise ValueError("Запись уже изменилась. Открой её заново; изменения не сохранены.")
            if entity == "session":
                pass_id = data.get("pass_id")
                if pass_id and not self.get_pass(uid, pass_id):
                    raise ValueError("Абонемент не найден.")
                tid = data.get("tournament_id")
                if tid and not self.tournament(uid, tid):
                    raise ValueError("Турнир не найден.")
                if data["kind"] == "match" and (not tid or data.get("cost", 0) or pass_id):
                    raise ValueError("Матч относится к турниру; его взнос учитывается в карточке турнира.")
                if pass_id and data["kind"] != "training":
                    raise ValueError("Абонемент можно выбрать только для тренировки.")
                if not record_id:
                    record_id = self.add(uid, data)
                self.db.execute("""UPDATE sessions SET played_on=?,minutes=?,cost_kopecks=?,participants=?,
                    coach=?,pass_id=?,kind=?,club=?,format=?,opponent=?,score=?,result=?,note=?,tournament_id=?
                    WHERE id=? AND user_id=?""",
                    (data["date"], data.get("minutes"), data.get("cost", 0), data.get("participants"),
                     data.get("coach"), pass_id, data["kind"], data.get("club"), data.get("format"),
                     data.get("opponent"), data.get("score"), data.get("result"), data.get("note"),
                     tid, record_id, uid))
            elif entity == "tournament":
                args = (data["name"], data["date"], data.get("club"), data.get("format"),
                        data["cost"], data.get("place"), data.get("note"))
                if record_id:
                    self.db.execute("""UPDATE tournaments SET name=?,played_on=?,club=?,format=?,fee=?,place=?,note=?
                        WHERE id=? AND user_id=?""", (*args, record_id, uid))
                else:
                    self.db.execute("""INSERT OR IGNORE INTO tournaments
                        (name,played_on,club,format,fee,place,note,user_id,token) VALUES (?,?,?,?,?,?,?,?,?)""",
                        (*args, uid, draft["token"]))
                    record_id = self.db.execute("SELECT id FROM tournaments WHERE token=? AND user_id=?",
                                                (draft["token"], uid)).fetchone()[0]
            elif entity == "expense":
                for key, reader in (("session_id", self.get_session), ("tournament_id", self.tournament)):
                    if data.get(key) and not reader(uid, data[key]):
                        raise ValueError("Связанная запись не найдена.")
                args = (data["date"], data["category"], data["cost"], data.get("note"),
                        data.get("session_id"), data.get("tournament_id"))
                if record_id:
                    self.db.execute("""UPDATE expenses SET played_on=?,category=?,amount=?,note=?,session_id=?,
                        tournament_id=? WHERE id=? AND user_id=?""", (*args, record_id, uid))
                else:
                    self.db.execute("""INSERT OR IGNORE INTO expenses
                        (played_on,category,amount,note,session_id,tournament_id,user_id,token)
                        VALUES (?,?,?,?,?,?,?,?)""", (*args, uid, draft["token"]))
                    record_id = self.db.execute("SELECT id FROM expenses WHERE token=? AND user_id=?",
                                                (draft["token"], uid)).fetchone()[0]
            elif entity == "deposit":
                if not self.get_pass(uid, data["pass_id"]):
                    raise ValueError("Абонемент не найден.")
                if record_id:
                    self.db.execute("UPDATE pass_deposits SET paid_on=?,amount=? WHERE id=?",
                                    (data.get("date"), data["cost"], record_id))
                else:
                    self.db.execute("INSERT OR IGNORE INTO pass_deposits(pass_id,amount,paid_on,token) VALUES (?,?,?,?)",
                                    (data["pass_id"], data["cost"], data.get("date"), draft["token"]))
                    record_id = self.db.execute("SELECT id FROM pass_deposits WHERE token=?", (draft["token"],)).fetchone()[0]
            else:
                raise ValueError("Неизвестный вид записи.")
        return record_id

    def remove_record(self, uid, entity, original):
        with self.pass_transaction():
            if self.owned_record(uid, entity, original["id"]) != original:
                raise ValueError("Запись уже изменилась. Открой её заново.")
            tables = {"session": "sessions", "tournament": "tournaments",
                      "expense": "expenses", "deposit": "pass_deposits"}
            # Owner was checked above; table names come only from this fixed mapping.
            self.db.execute("DELETE FROM " + tables[entity] + " WHERE id=?", (original["id"],))

    def all_passes(self, uid):
        return [self.get_pass(uid, r[0]) for r in self.db.execute(
            "SELECT id FROM passes WHERE user_id=? ORDER BY id DESC", (uid,))]

    def pass_sessions(self, uid, pass_id):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM sessions WHERE user_id=? AND pass_id=? ORDER BY played_on DESC,id DESC", (uid, pass_id))]

    def pass_deposits(self, uid, pass_id):
        return [dict(r) for r in self.db.execute("""SELECT d.* FROM pass_deposits d
            JOIN passes p ON p.id=d.pass_id WHERE p.user_id=? AND p.id=?
            ORDER BY d.paid_on DESC,d.id DESC""", (uid, pass_id))]

    def operations(self, uid, start, end, include_unknown=False):
        result = []
        for r in self.db.execute("""SELECT d.*,p.number FROM pass_deposits d JOIN passes p ON p.id=d.pass_id
            WHERE p.user_id=?""", (uid,)):
            if (r["paid_on"] is None and include_unknown) or (r["paid_on"] and start <= r["paid_on"] <= end):
                result.append(dict(entity="deposit", id=r["id"], date=r["paid_on"], amount=r["amount"],
                                   category="Клуб и тренировки", title="Пополнение · " + r["number"], cash=True))
        for r in self.records(uid, start, end):
            if r["kind"] == "match":
                continue
            paid = r["pass_id"] is None
            result.append(dict(entity="session", id=r["id"], date=r["played_on"], amount=r["cost_kopecks"],
                               category="Клуб и тренировки" if r["kind"] == "training" else "Самостоятельные игры",
                               title=("Тренировка" if r["kind"] == "training" else "Игра") +
                                     ("" if paid else " · списание с абонемента"), cash=paid))
        for r in self.tournaments_in(uid, start, end):
            result.append(dict(entity="tournament", id=r["id"], date=r["played_on"], amount=r["fee"],
                               category="Турниры", title=r["name"] + " · взнос", cash=True))
        for r in self.db.execute("SELECT * FROM expenses WHERE user_id=? AND played_on BETWEEN ? AND ?",
                                 (uid, start, end)):
            result.append(dict(entity="expense", id=r["id"], date=r["played_on"], amount=r["amount"],
                               category=r["category"], title=r["category"] + (": " + r["note"] if r["note"] else ""),
                               cash=True))
        return sorted(result, key=lambda r: (r["date"] or "", r["id"]), reverse=True)

    def finances(self, uid, start, end):
        operations = self.operations(uid, start, end)
        categories = defaultdict(int)
        for r in operations:
            if r["cash"]:
                categories[r["category"]] += r["amount"]
        passes = self.all_passes(uid)
        unknown = self.db.execute("""SELECT COALESCE(SUM(d.amount),0),COUNT(*) FROM pass_deposits d
            JOIN passes p ON p.id=d.pass_id WHERE p.user_id=? AND d.paid_on IS NULL""", (uid,)).fetchone()
        return {"paid": sum(categories.values()), "categories": dict(categories),
                "consumed": sum(r["cost_kopecks"] for r in self.records(uid, start, end)),
                "balance": sum(p["paid"] - p["used"] for p in passes),
                "unknown": unknown[0], "unknown_count": unknown[1]}
