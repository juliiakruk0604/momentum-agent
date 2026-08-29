from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:
    psycopg = None
    dict_row = None

SQLITE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  signal_time TEXT NOT NULL,
  available_time TEXT NOT NULL,
  signal_price REAL NOT NULL,
  impulse_json TEXT NOT NULL,
  continuation_json TEXT,
  readiness_json TEXT,
  labels_json TEXT,
  last_labeled_horizon INTEGER DEFAULT 0,
  state TEXT NOT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(symbol, signal_time)
);
CREATE TABLE IF NOT EXISTS runtime_state(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
'''

POSTGRES_SCHEMA = '''
CREATE TABLE IF NOT EXISTS events(
  id BIGSERIAL PRIMARY KEY,
  symbol TEXT NOT NULL,
  signal_time TEXT NOT NULL,
  available_time TEXT NOT NULL,
  signal_price DOUBLE PRECISION NOT NULL,
  impulse_json TEXT NOT NULL,
  continuation_json TEXT,
  readiness_json TEXT,
  labels_json TEXT,
  last_labeled_horizon INTEGER DEFAULT 0,
  state TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(symbol, signal_time)
);
CREATE TABLE IF NOT EXISTS runtime_state(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_events_state ON events(state);
CREATE INDEX IF NOT EXISTS idx_events_available_time ON events(available_time);
'''


class SignalStore:
    def __init__(self, path: str = "momentum.db", database_url: str | None = None):
        self.database_url = database_url or os.getenv("DATABASE_URL")
        self.path = Path(path)
        self.backend = "postgres" if self.database_url else "sqlite"
        if self.backend == "postgres" and psycopg is None:
            raise RuntimeError("DATABASE_URL is set but psycopg is not installed")
        self._ensure_schema()

    @contextmanager
    def _conn(self):
        if self.backend == "postgres":
            with psycopg.connect(self.database_url, row_factory=dict_row) as c:
                yield c
        else:
            c = sqlite3.connect(self.path)
            c.row_factory = sqlite3.Row
            try:
                yield c
                c.commit()
            finally:
                c.close()

    def _ensure_schema(self):
        if self.backend == "postgres":
            with self._conn() as c:
                with c.cursor() as cur:
                    cur.execute(POSTGRES_SCHEMA)
                c.commit()
        else:
            with self._conn() as c:
                c.executescript(SQLITE_SCHEMA)
                cols = {r[1] for r in c.execute("PRAGMA table_info(events)").fetchall()}
                if "labels_json" not in cols:
                    c.execute("ALTER TABLE events ADD COLUMN labels_json TEXT")
                if "last_labeled_horizon" not in cols:
                    c.execute("ALTER TABLE events ADD COLUMN last_labeled_horizon INTEGER DEFAULT 0")

    def _execute(self, sql_sqlite: str, sql_pg: str, params=(), fetch=None):
        with self._conn() as c:
            cur = c.cursor()
            cur.execute(sql_pg if self.backend == "postgres" else sql_sqlite, params)
            if fetch == "one":
                row = cur.fetchone()
                return None if row is None else dict(row)
            if fetch == "all":
                return [dict(r) for r in cur.fetchall()]
            if self.backend == "postgres":
                c.commit()
            return None

    def upsert_impulse(self, imp):
        payload = json.dumps(asdict(imp), default=str)
        params = (
            imp.symbol, str(imp.signal_time), str(imp.available_time),
            imp.signal_price, payload, "IMPULSE",
        )
        self._execute(
            '''INSERT OR IGNORE INTO events(symbol,signal_time,available_time,signal_price,impulse_json,state)
               VALUES(?,?,?,?,?,?)''',
            '''INSERT INTO events(symbol,signal_time,available_time,signal_price,impulse_json,state)
               VALUES(%s,%s,%s,%s,%s,%s)
               ON CONFLICT(symbol,signal_time) DO NOTHING''',
            params,
        )

    def pending(self):
        return self._execute(
            "SELECT * FROM events WHERE state='IMPULSE' ORDER BY available_time",
            "SELECT * FROM events WHERE state='IMPULSE' ORDER BY available_time",
            fetch="all",
        )

    def finalize(self, symbol, signal_time, cont, ready):
        params = (
            json.dumps(asdict(cont), default=str),
            json.dumps(asdict(ready), default=str),
            ready.state, symbol, str(signal_time),
        )
        self._execute(
            '''UPDATE events SET continuation_json=?,readiness_json=?,state=?,updated_at=CURRENT_TIMESTAMP
               WHERE symbol=? AND signal_time=?''',
            '''UPDATE events SET continuation_json=%s,readiness_json=%s,state=%s,updated_at=NOW()
               WHERE symbol=%s AND signal_time=%s''',
            params,
        )

    def labeling_candidates(self, limit: int = 250):
        return self._execute(
            '''SELECT * FROM events WHERE state<>'IMPULSE' AND COALESCE(last_labeled_horizon,0)<1440
               ORDER BY available_time LIMIT ?''',
            '''SELECT * FROM events WHERE state<>'IMPULSE' AND COALESCE(last_labeled_horizon,0)<1440
               ORDER BY available_time LIMIT %s''',
            (int(limit),),
            fetch="all",
        )

    def update_labels(self, symbol: str, signal_time, labels: list, last_horizon: int):
        params = (json.dumps(labels, default=str), int(last_horizon), symbol, str(signal_time))
        self._execute(
            '''UPDATE events SET labels_json=?,last_labeled_horizon=?,updated_at=CURRENT_TIMESTAMP
               WHERE symbol=? AND signal_time=?''',
            '''UPDATE events SET labels_json=%s,last_labeled_horizon=%s,updated_at=NOW()
               WHERE symbol=%s AND signal_time=%s''',
            params,
        )

    def recent(self, limit: int = 100):
        return self._execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?",
            "SELECT * FROM events ORDER BY id DESC LIMIT %s",
            (int(limit),),
            fetch="all",
        )

    def stats(self):
        rows = self._execute(
            "SELECT state,COUNT(*) AS n FROM events GROUP BY state ORDER BY n DESC",
            "SELECT state,COUNT(*) AS n FROM events GROUP BY state ORDER BY n DESC",
            fetch="all",
        )
        total = sum(int(r["n"]) for r in rows)
        return {"total_events": total, "by_state": rows, "backend": self.backend}

    def set_runtime(self, key: str, value):
        payload = json.dumps(value, default=str)
        self._execute(
            '''INSERT INTO runtime_state(key,value,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP''',
            '''INSERT INTO runtime_state(key,value,updated_at) VALUES(%s,%s,NOW())
               ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=NOW()''',
            (key, payload),
        )

    def get_runtime(self, key: str):
        row = self._execute(
            "SELECT value,updated_at FROM runtime_state WHERE key=?",
            "SELECT value,updated_at FROM runtime_state WHERE key=%s",
            (key,),
            fetch="one",
        )
        if row is None:
            return None
        try:
            value = json.loads(row["value"])
        except Exception:
            value = row["value"]
        return {"value": value, "updated_at": str(row["updated_at"])}

    def ping(self):
        with self._conn() as c:
            cur = c.cursor()
            cur.execute("SELECT 1")
            return bool(cur.fetchone())
