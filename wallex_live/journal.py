import fcntl
import json
import sqlite3
import time
import uuid
from pathlib import Path


class Journal:
    def __init__(self, directory, identity):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.lock = (directory / "writer.lock").open("a")
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.db = sqlite3.connect(directory / "live.sqlite3")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT);"
            "CREATE TABLE IF NOT EXISTS intents(id TEXT PRIMARY KEY,body TEXT,purpose TEXT,stage TEXT,created REAL,result TEXT,applied INTEGER DEFAULT 0);"
            "CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,at REAL,kind TEXT,payload TEXT);")
        previous = self.get("identity")
        if previous is not None and previous != identity:
            raise ValueError("journal_account_identity_changed")
        self.set("identity", identity)

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, json.dumps(value, default=str)))

    def event(self, kind, payload, **kwargs):
        with self.db:
            self.db.execute("INSERT INTO events(at,kind,payload) VALUES(?,?,?)",
                            (time.time(), kind, json.dumps(payload, default=str)))

    def prepare(self, body, purpose):
        cid = "KAV_" + uuid.uuid4().hex
        body = {**body, "client_id": cid}
        with self.db:
            self.db.execute("INSERT INTO intents(id,body,purpose,stage,created) VALUES(?,?,?,?,?)",
                            (cid, json.dumps(body), purpose, "prepared", time.time()))
        return cid, body

    def update(self, cid, stage, result=None):
        with self.db:
            self.db.execute("UPDATE intents SET stage=?,result=? WHERE id=?",
                            (stage, json.dumps(result, default=str), cid))

    def pending(self):
        rows = self.db.execute(
            "SELECT id,body,purpose,stage,created FROM intents WHERE stage NOT IN ('terminal','rejected') ORDER BY created").fetchall()
        return [{"id": r[0], "body": json.loads(r[1]), "purpose": r[2], "stage": r[3], "created": r[4]} for r in rows]

    def unapplied(self):
        return [(r[0], json.loads(r[1]), r[2], json.loads(r[3])) for r in self.db.execute(
            "SELECT id,body,purpose,result FROM intents WHERE stage='terminal' AND applied=0").fetchall()]

    def apply(self, cid, updates):
        with self.db:
            row = self.db.execute("SELECT applied FROM intents WHERE id=?", (cid,)).fetchone()
            if not row or row[0]:
                return
            for key, value in updates.items():
                self.db.execute("INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                (key, json.dumps(value, default=str)))
            self.db.execute("UPDATE intents SET applied=1 WHERE id=?", (cid,))

    def close(self):
        self.db.close()
        self.lock.close()
