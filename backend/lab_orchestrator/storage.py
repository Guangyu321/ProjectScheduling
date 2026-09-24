"""SQLite persistence and an OS-released local controller lock."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3


def encode(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':'))


def digest(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


class DatabaseInUseError(RuntimeError):
    """The controller lock is held by another process or Store instance."""
    def __init__(self,path):
        self.path=Path(path)
        super().__init__(f'Another controller owns this simulation database: {self.path}')


class Store:
    def __init__(self,path):
        self.path=Path(path).resolve()
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.db=None; self.lock=None
        self.lock=open(str(self.path)+'.lock','a+b')
        self.lock.seek(0,2)
        if self.lock.tell()==0:
            self.lock.write(b'0'); self.lock.flush()
        self.lock.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.lock.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            self.lock.close(); self.lock=None
            raise DatabaseInUseError(self.path) from exc
        try:
            self.db=sqlite3.connect(self.path,isolation_level=None)
            self.db.row_factory=sqlite3.Row
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY,request_key TEXT UNIQUE NOT NULL,
              payload_hash TEXT NOT NULL,body TEXT NOT NULL,state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS steps (id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id),
              body TEXT NOT NULL,state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS plates (id TEXT PRIMARY KEY,location TEXT UNIQUE NOT NULL,
              certainty TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY,step_id TEXT UNIQUE NOT NULL REFERENCES steps(id),
              state TEXT NOT NULL,started REAL NOT NULL,ended REAL,resources TEXT NOT NULL,result TEXT);
            CREATE TABLE IF NOT EXISTS locks (resource TEXT PRIMARY KEY,command_id TEXT NOT NULL REFERENCES commands(id));
            CREATE TABLE IF NOT EXISTS alarms (command_id TEXT PRIMARY KEY REFERENCES commands(id),reason TEXT NOT NULL,state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT,utc TEXT NOT NULL,
              sim_time REAL NOT NULL,kind TEXT NOT NULL,subject TEXT NOT NULL,body TEXT NOT NULL);
            ''')
            self._migrate()
        except BaseException:
            self.close(); raise

    def _migrate(self):
        """Preserve v0.1 rows while allowing multiple audited attempts per step."""
        columns={row[1] for row in self.db.execute('PRAGMA table_info(commands)')}
        if 'attempt' not in columns:
            self.db.execute('PRAGMA foreign_keys=OFF')
            try:
                with self.transaction() as db:
                    db.execute("""CREATE TABLE commands_v2 (
                        id TEXT PRIMARY KEY,step_id TEXT NOT NULL REFERENCES steps(id),
                        state TEXT NOT NULL,started REAL NOT NULL,ended REAL,
                        resources TEXT NOT NULL,result TEXT,attempt INTEGER NOT NULL DEFAULT 1,
                        UNIQUE(step_id,attempt))""")
                    db.execute('INSERT INTO commands_v2 SELECT *,1 FROM commands')
                    db.execute('DROP TABLE commands')
                    db.execute('ALTER TABLE commands_v2 RENAME TO commands')
            finally:
                self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute("""CREATE TABLE IF NOT EXISTS recovery_cases (
            command_id TEXT PRIMARY KEY REFERENCES commands(id),state TEXT NOT NULL,
            decision TEXT,actor TEXT,reason TEXT NOT NULL,evidence TEXT NOT NULL)""")
        self.db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_live_command_per_step
            ON commands(step_id) WHERE state NOT IN ('succeeded','not_started','cancelled')""")
        if self.db.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('Database migration found inconsistent references')

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield self.db
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    def event(self,now,kind,subject,body=None):
        self.db.execute('INSERT INTO events(utc,sim_time,kind,subject,body) VALUES(?,?,?,?,?)',
          (datetime.now(timezone.utc).isoformat(),now,kind,subject,encode(body or {})))

    def meta(self,key,default=None):
        row=self.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self,key,value):
        self.db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,encode(value)))

    def close(self):
        if self.db is not None:
            self.db.close(); self.db=None
        if self.lock is not None:
            self.lock.seek(0)
            try:
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(self.lock.fileno(),msvcrt.LK_UNLCK,1)
                else:
                    import fcntl
                    fcntl.flock(self.lock.fileno(),fcntl.LOCK_UN)
            finally:
                self.lock.close(); self.lock=None
