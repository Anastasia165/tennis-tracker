"""SQLite persistence. Amounts are integer kopecks; dates are local ISO dates."""
import json
from contextlib import contextmanager
import sqlite3
from pathlib import Path
from tennis_store import TennisStore


class Store(TennisStore):
    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS activities (
                id TEXT PRIMARY KEY, title TEXT NOT NULL
            );
            INSERT OR IGNORE INTO activities VALUES ('tennis', 'Теннис');
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                activity_id TEXT NOT NULL REFERENCES activities(id),
                played_on TEXT NOT NULL,
                minutes INTEGER NOT NULL CHECK(minutes BETWEEN 1 AND 1440),
                cost_kopecks INTEGER NOT NULL CHECK(cost_kopecks >= 0),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS sessions_period
                ON sessions(user_id, activity_id, played_on);
            CREATE TABLE IF NOT EXISTS drafts (
                user_id INTEGER PRIMARY KEY, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS processed_updates (
                id INTEGER PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL
            );
        """)
        self.migrate()
        self.migrate_tennis()

    def migrate(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS passes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                number TEXT NOT NULL, source TEXT NOT NULL, import_token TEXT UNIQUE NOT NULL,
                notes TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS pass_deposits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pass_id INTEGER NOT NULL REFERENCES passes(id) ON DELETE CASCADE,
                amount INTEGER NOT NULL CHECK(amount >= 0)
            );
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(sessions)")}
        self.db.execute("BEGIN")
        with self.db:
            if "participants" not in columns:
                self.db.execute("ALTER TABLE sessions ADD COLUMN participants INTEGER CHECK(participants BETWEEN 1 AND 100)")
            if "coach" not in columns:
                self.db.execute("ALTER TABLE sessions ADD COLUMN coach TEXT")
            if "pass_id" not in columns:
                self.db.execute("ALTER TABLE sessions ADD COLUMN pass_id INTEGER REFERENCES passes(id)")
            info = list(self.db.execute("PRAGMA table_info(sessions)"))
            if next(row for row in info if row[1] == "minutes")[3]:
                sequence = self.db.execute("SELECT seq FROM sqlite_sequence WHERE name=\'sessions\'").fetchone()
                # Rebuild once to allow genuinely unknown historical durations.
                self.db.execute("""CREATE TABLE sessions_nullable (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                    activity_id TEXT NOT NULL REFERENCES activities(id), played_on TEXT NOT NULL,
                    minutes INTEGER CHECK(minutes BETWEEN 1 AND 1440),
                    cost_kopecks INTEGER NOT NULL CHECK(cost_kopecks >= 0),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    participants INTEGER CHECK(participants BETWEEN 1 AND 100), coach TEXT,
                    pass_id INTEGER REFERENCES passes(id))""")
                self.db.execute("""INSERT INTO sessions_nullable
                    SELECT id,user_id,activity_id,played_on,minutes,cost_kopecks,
                           created_at,participants,coach,pass_id FROM sessions""")
                self.db.execute("DROP TABLE sessions")
                self.db.execute("ALTER TABLE sessions_nullable RENAME TO sessions")
                self.db.execute("CREATE INDEX sessions_period ON sessions(user_id,activity_id,played_on)")
                if sequence:
                    self.db.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name=\'sessions\'", (sequence[0],))

    @contextmanager
    def pass_transaction(self):
        if not self.db.in_transaction:
            self.db.execute("BEGIN")
        self.db.execute("SAVEPOINT pass_change")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK TO pass_change")
            self.db.execute("RELEASE pass_change")
            raise
        else:
            self.db.execute("RELEASE pass_change")

    def get_pass(self, user_id, pass_id):
        row = self.db.execute("""
            SELECT p.*, COALESCE((SELECT SUM(amount) FROM pass_deposits WHERE pass_id=p.id),0) AS paid,
                COALESCE((SELECT SUM(cost_kopecks) FROM sessions WHERE pass_id=p.id),0) AS used
            FROM passes p WHERE p.user_id=? AND p.id=?
        """, (user_id, pass_id)).fetchone()
        return dict(row) if row else None

    def unlinked(self, user_id, offset=0):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM sessions WHERE user_id=? AND activity_id='tennis' AND pass_id IS NULL AND kind='training' "
            "ORDER BY played_on DESC,id DESC LIMIT 20 OFFSET ?", (user_id, offset))]

    def validate_links(self, user_id, selections):
        from linking import snapshot
        seen = set()
        for expected in selections:
            row = self.get_session(user_id, expected["id"])
            if (expected["id"] in seen or not row or row["pass_id"] is not None or row["kind"] != "training"
                    or snapshot(row) != expected):
                raise ValueError("Выбранные занятия изменились или уже привязаны. Выбери их заново; ничего не сохранено.")
            seen.add(expected["id"])

    def link_sessions(self, user_id, pass_id, selections):
        with self.pass_transaction():
            if not self.get_pass(user_id, pass_id):
                raise ValueError("Абонемент не найден.")
            self.validate_links(user_id, selections)
            for row in selections:
                changed = self.db.execute(
                    "UPDATE sessions SET pass_id=? WHERE id=? AND user_id=? AND pass_id IS NULL",
                    (pass_id, row["id"], user_id)).rowcount
                if changed != 1:
                    raise ValueError("Занятие уже привязано. Выбери занятия заново.")

    def on_date(self, user_id, played_on):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM sessions WHERE user_id=? AND activity_id='tennis' AND played_on=? ORDER BY id",
            (user_id, played_on))]

    def pass_conflicts(self, user_id, draft):
        conflicts = []
        seen = set()
        for index, row in enumerate(draft["sessions"], 1):
            key = row["date"]
            existing = self.on_date(user_id, key)
            if row.get("existing"):
                existing = [r for r in existing if r["id"] != row["existing"]["id"]]
            if existing or key in seen:
                conflicts.append("строка " + str(index) + " (" + key + ")")
            seen.add(key)
        old = self.db.execute("SELECT 1 FROM passes WHERE user_id=? AND number=?",
                              (user_id, draft["number"])).fetchone()
        if old:
            conflicts.append("абонемент с таким номером уже есть")
        return conflicts

    def save_pass(self, user_id, draft):
        with self.pass_transaction():
            return self._save_pass(user_id, draft)

    def _save_pass(self, user_id, draft):
        existing = self.db.execute("SELECT id FROM passes WHERE import_token=? AND user_id=?",
                                   (draft["token"], user_id)).fetchone()
        if existing:
            return existing[0]
        links = [row["existing"] for row in draft["sessions"] if row.get("existing")]
        self.validate_links(user_id, links)
        for row in draft["sessions"]:
            if row.get("existing"):
                expected = row["existing"]
                if (row["date"], row["cost"], row["minutes"], row["participants"], row["coach"]) != (
                        expected["played_on"], expected["cost_kopecks"], expected["minutes"],
                        expected["participants"], expected["coach"]):
                    raise ValueError("Связанная строка изменилась. Выбери существующее занятие заново.")
        # Caller transaction includes all sessions, deposits and the draft/update marker.
        cursor = self.db.execute(
            "INSERT INTO passes(user_id,number,source,import_token,notes) VALUES(?,?,?,?,?)",
            (user_id, draft["number"], draft["source"], draft["token"],
             json.dumps(draft.get("notes", []), ensure_ascii=False)))
        pass_id = cursor.lastrowid
        club = self.preferences(user_id).get("club")
        self.db.execute("UPDATE passes SET club=? WHERE id=?", (club, pass_id))
        self.db.executemany("INSERT INTO pass_deposits(pass_id,amount) VALUES(?,?)",
                            [(pass_id, amount) for amount in draft["deposits"]])
        for row in draft["sessions"]:
            if row.get("existing"):
                self.link_sessions(user_id, pass_id, [row["existing"]])
            else:
                session_id = self.add(user_id, row)
                self.db.execute("UPDATE sessions SET pass_id=?,club=? WHERE id=?", (pass_id, club, session_id))
        return pass_id

    def passes(self, user_id):
        return [dict(row) for row in self.db.execute("""
            SELECT p.*, COALESCE((SELECT SUM(amount) FROM pass_deposits WHERE pass_id=p.id),0) AS paid,
                COALESCE((SELECT SUM(cost_kopecks) FROM sessions WHERE pass_id=p.id),0) AS used,
                (SELECT COUNT(*) FROM sessions WHERE pass_id=p.id) AS visits
            FROM passes p WHERE user_id=? ORDER BY p.id DESC LIMIT 20
        """, (user_id,))]

    def coaches(self, user_id):
        return [row[0] for row in self.db.execute(
            "SELECT coach FROM sessions WHERE user_id=? AND activity_id='tennis' "
            "AND coach IS NOT NULL AND coach != '' GROUP BY coach ORDER BY MAX(id) DESC LIMIT 6",
            (user_id,))]

    def close(self):
        self.db.close()

    def draft(self, user_id):
        row = self.db.execute("SELECT data FROM drafts WHERE user_id=?", (user_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_draft(self, user_id, data):
        if data is None:
            self.db.execute("DELETE FROM drafts WHERE user_id=?", (user_id,))
        else:
            self.db.execute(
                "INSERT OR REPLACE INTO drafts VALUES (?, ?)",
                (user_id, json.dumps(data, ensure_ascii=False)),
            )

    def add(self, user_id, draft):
        cursor = self.db.execute(
            "INSERT INTO sessions(user_id,activity_id,played_on,minutes,cost_kopecks,participants,coach) "
            "VALUES (?,?,?,?,?,?,?)",
            (user_id, "tennis", draft["date"], draft["minutes"], draft["cost"],
             draft.get("participants"), draft.get("coach")),
        )
        return cursor.lastrowid

    def get_session(self, user_id, session_id):
        row = self.db.execute(
            "SELECT * FROM sessions WHERE user_id=? AND id=? AND activity_id='tennis'",
            (user_id, session_id),
        ).fetchone()
        return dict(row) if row else None

    def delete(self, user_id, session_id):
        return self.db.execute(
            "DELETE FROM sessions WHERE id=? AND user_id=? AND activity_id='tennis'",
            (session_id, user_id),
        ).rowcount

    def history(self, user_id):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM sessions WHERE user_id=? AND activity_id='tennis' "
            "AND kind='training' ORDER BY played_on DESC,id DESC LIMIT 10", (user_id,)
        )]

    def month(self, user_id, month):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM sessions WHERE user_id=? AND activity_id='tennis' "
            "AND kind='training' AND played_on>=? AND played_on<=? ORDER BY played_on,id",
            (user_id, month + "-01", month + "-31"),
        )]

    def offset(self):
        row = self.db.execute("SELECT value FROM metadata WHERE key='offset'").fetchone()
        return int(row[0]) if row else 0

    def processed(self, update_id):
        return self.db.execute(
            "SELECT 1 FROM processed_updates WHERE id=?", (update_id,)
        ).fetchone() is not None

    def mark_processed(self, update_id):
        self.db.execute("INSERT INTO processed_updates VALUES (?)", (update_id,))
        self.db.execute(
            "INSERT OR REPLACE INTO metadata VALUES ('offset',?)", (str(update_id + 1),)
        )

    def enqueue(self, chat_id, kind, payload):
        self.db.execute(
            "INSERT INTO outbox(chat_id,kind,payload) VALUES (?,?,?)",
            (chat_id, kind, json.dumps(payload, ensure_ascii=False)),
        )

    def pending(self):
        row = self.db.execute("SELECT * FROM outbox ORDER BY id LIMIT 1").fetchone()
        if row:
            return {**dict(row), "payload": json.loads(row["payload"])}
        return None

    def delivered(self, row_id):
        with self.db:
            self.db.execute("DELETE FROM outbox WHERE id=?", (row_id,))

    def backup(self, destination):
        if str(destination) == ":memory:":
            raise ValueError("Укажите путь к файлу резервной копии.")
        target = Path(destination)
        # Do not silently overwrite an existing backup.
        with target.open("xb"):
            pass
        backup_db = sqlite3.connect(target)
        try:
            self.db.backup(backup_db)
        finally:
            backup_db.close()
