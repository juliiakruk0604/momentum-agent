from __future__ import annotations

import json
import os
import sqlite3
import statistics
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:  # pragma: no cover - optional in local fallback
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
  derivatives_json TEXT,
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
CREATE TABLE IF NOT EXISTS research_snapshots(
  snapshot_date TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS historical_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dataset_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  signal_time TEXT NOT NULL,
  available_time TEXT NOT NULL,
  signal_price REAL NOT NULL,
  impulse_json TEXT NOT NULL,
  continuation_json TEXT,
  derivatives_json TEXT,
  labels_json TEXT,
  fold_id INTEGER,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(dataset_id, symbol, signal_time)
);
CREATE TABLE IF NOT EXISTS historical_symbol_runs(
  dataset_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  status TEXT NOT NULL,
  bars15 INTEGER DEFAULT 0,
  impulses INTEGER DEFAULT 0,
  oi_rows INTEGER DEFAULT 0,
  funding_rows INTEGER DEFAULT 0,
  error TEXT,
  processed_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(dataset_id, symbol)
);
CREATE TABLE IF NOT EXISTS v22_flow_snapshots(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_time TEXT NOT NULL,
  symbol TEXT NOT NULL,
  signal_time TEXT,
  price REAL,
  score REAL,
  regime TEXT,
  action TEXT,
  fast_json TEXT,
  book_json TEXT,
  trade_flow_json TEXT,
  payload_json TEXT NOT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(symbol, snapshot_time)
);
CREATE INDEX IF NOT EXISTS idx_v22_flow_time ON v22_flow_snapshots(snapshot_time);
CREATE INDEX IF NOT EXISTS idx_v22_flow_symbol_time ON v22_flow_snapshots(symbol,snapshot_time);
CREATE TABLE IF NOT EXISTS v22_flow_labels(
  symbol TEXT NOT NULL,
  snapshot_time TEXT NOT NULL,
  horizon_minutes INTEGER NOT NULL,
  entry_price REAL NOT NULL,
  final_return_pct REAL,
  mfe_pct REAL,
  mae_pct REAL,
  hit_0_5 INTEGER DEFAULT 0,
  hit_1 INTEGER DEFAULT 0,
  hit_2 INTEGER DEFAULT 0,
  hit_5 INTEGER DEFAULT 0,
  hit_10 INTEGER DEFAULT 0,
  payload_json TEXT NOT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(symbol,snapshot_time,horizon_minutes)
);
CREATE INDEX IF NOT EXISTS idx_v22_flow_labels_horizon ON v22_flow_labels(horizon_minutes);
CREATE TABLE IF NOT EXISTS v24_feature_snapshots(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_ms INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  best_bid REAL,
  best_ask REAL,
  mid REAL,
  microstructure_score REAL,
  regime TEXT,
  payload_json TEXT NOT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(symbol,snapshot_ms)
);
CREATE INDEX IF NOT EXISTS idx_v24_feature_symbol_ms ON v24_feature_snapshots(symbol,snapshot_ms);
CREATE INDEX IF NOT EXISTS idx_v24_feature_ms ON v24_feature_snapshots(snapshot_ms);
CREATE TABLE IF NOT EXISTS v24_price_ticks(
  snapshot_ms INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  best_bid REAL NOT NULL,
  best_ask REAL NOT NULL,
  mid REAL NOT NULL,
  PRIMARY KEY(symbol,snapshot_ms)
);
CREATE INDEX IF NOT EXISTS idx_v24_price_symbol_ms ON v24_price_ticks(symbol,snapshot_ms);
CREATE INDEX IF NOT EXISTS idx_v24_price_ms ON v24_price_ticks(snapshot_ms);
CREATE TABLE IF NOT EXISTS v24_feature_labels(
  symbol TEXT NOT NULL,
  snapshot_ms INTEGER NOT NULL,
  horizon_seconds INTEGER NOT NULL,
  entry_ask REAL NOT NULL,
  final_bid_return_pct REAL,
  mfe_bid_pct REAL,
  mae_bid_pct REAL,
  hit_0_1 INTEGER DEFAULT 0,
  hit_0_25 INTEGER DEFAULT 0,
  hit_0_5 INTEGER DEFAULT 0,
  hit_1 INTEGER DEFAULT 0,
  hit_2 INTEGER DEFAULT 0,
  payload_json TEXT NOT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(symbol,snapshot_ms,horizon_seconds)
);
CREATE INDEX IF NOT EXISTS idx_v24_labels_horizon ON v24_feature_labels(horizon_seconds);
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
  derivatives_json TEXT,
  readiness_json TEXT,
  labels_json TEXT,
  last_labeled_horizon INTEGER DEFAULT 0,
  state TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(symbol, signal_time)
);
ALTER TABLE events ADD COLUMN IF NOT EXISTS derivatives_json TEXT;
CREATE TABLE IF NOT EXISTS runtime_state(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS research_snapshots(
  snapshot_date DATE PRIMARY KEY,
  payload TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS historical_events(
  id BIGSERIAL PRIMARY KEY,
  dataset_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  signal_time TEXT NOT NULL,
  available_time TEXT NOT NULL,
  signal_price DOUBLE PRECISION NOT NULL,
  impulse_json TEXT NOT NULL,
  continuation_json TEXT,
  derivatives_json TEXT,
  labels_json TEXT,
  fold_id INTEGER,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(dataset_id, symbol, signal_time)
);
CREATE TABLE IF NOT EXISTS historical_symbol_runs(
  dataset_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  status TEXT NOT NULL,
  bars15 INTEGER DEFAULT 0,
  impulses INTEGER DEFAULT 0,
  oi_rows INTEGER DEFAULT 0,
  funding_rows INTEGER DEFAULT 0,
  error TEXT,
  processed_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY(dataset_id, symbol)
);
CREATE TABLE IF NOT EXISTS v22_flow_snapshots(
  id BIGSERIAL PRIMARY KEY,
  snapshot_time TIMESTAMPTZ NOT NULL,
  symbol TEXT NOT NULL,
  signal_time TIMESTAMPTZ,
  price DOUBLE PRECISION,
  score DOUBLE PRECISION,
  regime TEXT,
  action TEXT,
  fast_json TEXT,
  book_json TEXT,
  trade_flow_json TEXT,
  payload_json TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(symbol, snapshot_time)
);
CREATE INDEX IF NOT EXISTS idx_v22_flow_time ON v22_flow_snapshots(snapshot_time);
CREATE INDEX IF NOT EXISTS idx_v22_flow_symbol_time ON v22_flow_snapshots(symbol,snapshot_time);
CREATE TABLE IF NOT EXISTS v22_flow_labels(
  symbol TEXT NOT NULL,
  snapshot_time TIMESTAMPTZ NOT NULL,
  horizon_minutes INTEGER NOT NULL,
  entry_price DOUBLE PRECISION NOT NULL,
  final_return_pct DOUBLE PRECISION,
  mfe_pct DOUBLE PRECISION,
  mae_pct DOUBLE PRECISION,
  hit_0_5 BOOLEAN DEFAULT FALSE,
  hit_1 BOOLEAN DEFAULT FALSE,
  hit_2 BOOLEAN DEFAULT FALSE,
  hit_5 BOOLEAN DEFAULT FALSE,
  hit_10 BOOLEAN DEFAULT FALSE,
  payload_json TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY(symbol,snapshot_time,horizon_minutes)
);
CREATE INDEX IF NOT EXISTS idx_v22_flow_labels_horizon ON v22_flow_labels(horizon_minutes);
CREATE TABLE IF NOT EXISTS v24_feature_snapshots(
  id BIGSERIAL PRIMARY KEY,
  snapshot_ms BIGINT NOT NULL,
  symbol TEXT NOT NULL,
  best_bid DOUBLE PRECISION,
  best_ask DOUBLE PRECISION,
  mid DOUBLE PRECISION,
  microstructure_score DOUBLE PRECISION,
  regime TEXT,
  payload_json TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(symbol,snapshot_ms)
);
CREATE INDEX IF NOT EXISTS idx_v24_feature_symbol_ms ON v24_feature_snapshots(symbol,snapshot_ms);
CREATE INDEX IF NOT EXISTS idx_v24_feature_ms ON v24_feature_snapshots(snapshot_ms);
CREATE TABLE IF NOT EXISTS v24_price_ticks(
  snapshot_ms BIGINT NOT NULL,
  symbol TEXT NOT NULL,
  best_bid DOUBLE PRECISION NOT NULL,
  best_ask DOUBLE PRECISION NOT NULL,
  mid DOUBLE PRECISION NOT NULL,
  PRIMARY KEY(symbol,snapshot_ms)
);
CREATE INDEX IF NOT EXISTS idx_v24_price_symbol_ms ON v24_price_ticks(symbol,snapshot_ms);
CREATE INDEX IF NOT EXISTS idx_v24_price_ms ON v24_price_ticks(snapshot_ms);
CREATE TABLE IF NOT EXISTS v24_feature_labels(
  symbol TEXT NOT NULL,
  snapshot_ms BIGINT NOT NULL,
  horizon_seconds INTEGER NOT NULL,
  entry_ask DOUBLE PRECISION NOT NULL,
  final_bid_return_pct DOUBLE PRECISION,
  mfe_bid_pct DOUBLE PRECISION,
  mae_bid_pct DOUBLE PRECISION,
  hit_0_1 BOOLEAN DEFAULT FALSE,
  hit_0_25 BOOLEAN DEFAULT FALSE,
  hit_0_5 BOOLEAN DEFAULT FALSE,
  hit_1 BOOLEAN DEFAULT FALSE,
  hit_2 BOOLEAN DEFAULT FALSE,
  payload_json TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY(symbol,snapshot_ms,horizon_seconds)
);
CREATE INDEX IF NOT EXISTS idx_v24_labels_horizon ON v24_feature_labels(horizon_seconds);
CREATE INDEX IF NOT EXISTS idx_historical_events_dataset ON historical_events(dataset_id);
CREATE INDEX IF NOT EXISTS idx_historical_events_fold ON historical_events(dataset_id,fold_id);
CREATE INDEX IF NOT EXISTS idx_historical_symbol_runs_dataset ON historical_symbol_runs(dataset_id);
CREATE INDEX IF NOT EXISTS idx_events_state ON events(state);
CREATE INDEX IF NOT EXISTS idx_events_available_time ON events(available_time);
CREATE INDEX IF NOT EXISTS idx_events_last_labeled_horizon ON events(last_labeled_horizon);
'''


def _loads(value):
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return None


def _utc_now():
    return datetime.now(timezone.utc)


def _parse_utc(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            try:
                dt = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class SignalStore:
    """Persistent forward-shadow dataset store.

    PostgreSQL is used on Railway through DATABASE_URL. SQLite is retained only
    for local development and unit tests.
    """

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
                if "derivatives_json" not in cols:
                    c.execute("ALTER TABLE events ADD COLUMN derivatives_json TEXT")

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

    def finalize(self, symbol, signal_time, cont, ready, derivatives=None):
        deriv_json = None if derivatives is None else json.dumps(asdict(derivatives), default=str)
        params = (
            json.dumps(asdict(cont), default=str), deriv_json,
            json.dumps(asdict(ready), default=str), ready.state,
            symbol, str(signal_time),
        )
        self._execute(
            '''UPDATE events SET continuation_json=?,derivatives_json=?,readiness_json=?,state=?,updated_at=CURRENT_TIMESTAMP
               WHERE symbol=? AND signal_time=?''',
            '''UPDATE events SET continuation_json=%s,derivatives_json=%s,readiness_json=%s,state=%s,updated_at=NOW()
               WHERE symbol=%s AND signal_time=%s''',
            params,
        )

    def labeling_candidates(self, limit: int = 250):
        return self._execute(
            '''SELECT * FROM events WHERE state<>'IMPULSE' AND COALESCE(last_labeled_horizon,0)<1440
               ORDER BY available_time LIMIT ?''',
            '''SELECT * FROM events WHERE state<>'IMPULSE' AND COALESCE(last_labeled_horizon,0)<1440
               ORDER BY available_time LIMIT %s''',
            (int(limit),), fetch="all",
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
            (int(limit),), fetch="all",
        )

    def stats(self):
        rows = self._execute(
            "SELECT state,COUNT(*) AS n FROM events GROUP BY state ORDER BY n DESC",
            "SELECT state,COUNT(*) AS n FROM events GROUP BY state ORDER BY n DESC",
            fetch="all",
        )
        total = sum(int(r["n"]) for r in rows)
        labeled_24h = self._execute(
            "SELECT COUNT(*) AS n FROM events WHERE COALESCE(last_labeled_horizon,0)>=1440",
            "SELECT COUNT(*) AS n FROM events WHERE COALESCE(last_labeled_horizon,0)>=1440",
            fetch="one",
        )
        return {
            "total_events": total,
            "fully_labeled_24h": int((labeled_24h or {}).get("n", 0)),
            "by_state": rows,
            "backend": self.backend,
        }

    def upsert_v22_flow_snapshot(self, snapshot: dict):
        snapshot_time = str(snapshot.get("snapshot_time") or snapshot.get("generated_at") or _utc_now())
        symbol = str(snapshot.get("symbol") or "")
        if not symbol:
            raise ValueError("v22 snapshot requires symbol")
        fast = snapshot.get("fast_features") or {}
        signal_time = snapshot.get("signal_time") or fast.get("signal_time")
        params = (
            snapshot_time,
            symbol,
            None if signal_time is None else str(signal_time),
            float(snapshot.get("signal_price") or fast.get("price") or 0.0),
            float(snapshot.get("score") or 0.0),
            str(snapshot.get("regime") or ""),
            str(snapshot.get("action") or ""),
            json.dumps(fast, default=str),
            json.dumps(snapshot.get("book") or {}, default=str),
            json.dumps(snapshot.get("trade_flow") or {}, default=str),
            json.dumps(snapshot, default=str),
        )
        self._execute(
            '''INSERT INTO v22_flow_snapshots(
                 snapshot_time,symbol,signal_time,price,score,regime,action,
                 fast_json,book_json,trade_flow_json,payload_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol,snapshot_time) DO UPDATE SET
                 signal_time=excluded.signal_time,price=excluded.price,score=excluded.score,
                 regime=excluded.regime,action=excluded.action,fast_json=excluded.fast_json,
                 book_json=excluded.book_json,trade_flow_json=excluded.trade_flow_json,
                 payload_json=excluded.payload_json''',
            '''INSERT INTO v22_flow_snapshots(
                 snapshot_time,symbol,signal_time,price,score,regime,action,
                 fast_json,book_json,trade_flow_json,payload_json
               ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(symbol,snapshot_time) DO UPDATE SET
                 signal_time=EXCLUDED.signal_time,price=EXCLUDED.price,score=EXCLUDED.score,
                 regime=EXCLUDED.regime,action=EXCLUDED.action,fast_json=EXCLUDED.fast_json,
                 book_json=EXCLUDED.book_json,trade_flow_json=EXCLUDED.trade_flow_json,
                 payload_json=EXCLUDED.payload_json''',
            params,
        )

    def v22_flow_snapshot_stats(self):
        total = self._execute(
            "SELECT COUNT(*) AS n FROM v22_flow_snapshots",
            "SELECT COUNT(*) AS n FROM v22_flow_snapshots",
            fetch="one",
        )
        symbols = self._execute(
            "SELECT COUNT(DISTINCT symbol) AS n FROM v22_flow_snapshots",
            "SELECT COUNT(DISTINCT symbol) AS n FROM v22_flow_snapshots",
            fetch="one",
        )
        latest = self._execute(
            "SELECT MAX(snapshot_time) AS ts FROM v22_flow_snapshots",
            "SELECT MAX(snapshot_time) AS ts FROM v22_flow_snapshots",
            fetch="one",
        )
        return {
            "snapshots": int((total or {}).get("n") or 0),
            "symbols": int((symbols or {}).get("n") or 0),
            "latest_snapshot_time": None if not latest else str(latest.get("ts")),
        }

    def v22_flow_label_candidates(self, horizon_minutes: int, limit: int = 100):
        cutoff = _utc_now().timestamp() - int(horizon_minutes) * 60
        cutoff_dt = datetime.fromtimestamp(cutoff, tz=timezone.utc)
        return self._execute(
            '''SELECT s.* FROM v22_flow_snapshots s
               LEFT JOIN v22_flow_labels l
                 ON l.symbol=s.symbol AND l.snapshot_time=s.snapshot_time
                AND l.horizon_minutes=?
               WHERE s.snapshot_time<=? AND l.symbol IS NULL
               ORDER BY s.snapshot_time ASC LIMIT ?''',
            '''SELECT s.* FROM v22_flow_snapshots s
               LEFT JOIN v22_flow_labels l
                 ON l.symbol=s.symbol AND l.snapshot_time=s.snapshot_time
                AND l.horizon_minutes=%s
               WHERE s.snapshot_time<=%s AND l.symbol IS NULL
               ORDER BY s.snapshot_time ASC LIMIT %s''',
            (int(horizon_minutes), cutoff_dt, int(limit)),
            fetch="all",
        )

    def upsert_v22_flow_label(self, label: dict):
        params = (
            str(label["symbol"]),
            str(label["snapshot_time"]),
            int(label["horizon_minutes"]),
            float(label["entry_price"]),
            float(label.get("final_return_pct") or 0.0),
            float(label.get("mfe_pct") or 0.0),
            float(label.get("mae_pct") or 0.0),
            bool(label.get("hit_0_5")),
            bool(label.get("hit_1")),
            bool(label.get("hit_2")),
            bool(label.get("hit_5")),
            bool(label.get("hit_10")),
            json.dumps(label, default=str),
        )
        self._execute(
            '''INSERT INTO v22_flow_labels(
                 symbol,snapshot_time,horizon_minutes,entry_price,final_return_pct,mfe_pct,mae_pct,
                 hit_0_5,hit_1,hit_2,hit_5,hit_10,payload_json,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(symbol,snapshot_time,horizon_minutes) DO UPDATE SET
                 entry_price=excluded.entry_price,final_return_pct=excluded.final_return_pct,
                 mfe_pct=excluded.mfe_pct,mae_pct=excluded.mae_pct,hit_0_5=excluded.hit_0_5,
                 hit_1=excluded.hit_1,hit_2=excluded.hit_2,hit_5=excluded.hit_5,
                 hit_10=excluded.hit_10,payload_json=excluded.payload_json,
                 updated_at=CURRENT_TIMESTAMP''',
            '''INSERT INTO v22_flow_labels(
                 symbol,snapshot_time,horizon_minutes,entry_price,final_return_pct,mfe_pct,mae_pct,
                 hit_0_5,hit_1,hit_2,hit_5,hit_10,payload_json,updated_at
               ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
               ON CONFLICT(symbol,snapshot_time,horizon_minutes) DO UPDATE SET
                 entry_price=EXCLUDED.entry_price,final_return_pct=EXCLUDED.final_return_pct,
                 mfe_pct=EXCLUDED.mfe_pct,mae_pct=EXCLUDED.mae_pct,hit_0_5=EXCLUDED.hit_0_5,
                 hit_1=EXCLUDED.hit_1,hit_2=EXCLUDED.hit_2,hit_5=EXCLUDED.hit_5,
                 hit_10=EXCLUDED.hit_10,payload_json=EXCLUDED.payload_json,
                 updated_at=NOW()''',
            params,
        )

    def v22_flow_label_stats(self):
        rows = self._execute(
            '''SELECT horizon_minutes,COUNT(*) AS n,
                      AVG(final_return_pct) AS avg_final,
                      AVG(mfe_pct) AS avg_mfe,
                      AVG(mae_pct) AS avg_mae,
                      AVG(hit_1) AS p_hit_1,
                      AVG(hit_2) AS p_hit_2,
                      AVG(hit_5) AS p_hit_5,
                      AVG(hit_10) AS p_hit_10
               FROM v22_flow_labels GROUP BY horizon_minutes ORDER BY horizon_minutes''',
            '''SELECT horizon_minutes,COUNT(*) AS n,
                      AVG(final_return_pct) AS avg_final,
                      AVG(mfe_pct) AS avg_mfe,
                      AVG(mae_pct) AS avg_mae,
                      AVG((hit_1)::int) AS p_hit_1,
                      AVG((hit_2)::int) AS p_hit_2,
                      AVG((hit_5)::int) AS p_hit_5,
                      AVG((hit_10)::int) AS p_hit_10
               FROM v22_flow_labels GROUP BY horizon_minutes ORDER BY horizon_minutes''',
            fetch="all",
        )
        return rows

    def v22_labeled_snapshots(self, horizon_minutes: int, limit: int = 5000):
        rows = self._execute(
            '''SELECT s.snapshot_time,s.symbol,s.payload_json,l.payload_json AS label_json
               FROM v22_flow_snapshots s
               JOIN v22_flow_labels l
                 ON l.symbol=s.symbol AND l.snapshot_time=s.snapshot_time
               WHERE l.horizon_minutes=?
               ORDER BY s.snapshot_time ASC LIMIT ?''',
            '''SELECT s.snapshot_time,s.symbol,s.payload_json,l.payload_json AS label_json
               FROM v22_flow_snapshots s
               JOIN v22_flow_labels l
                 ON l.symbol=s.symbol AND l.snapshot_time=s.snapshot_time
               WHERE l.horizon_minutes=%s
               ORDER BY s.snapshot_time ASC LIMIT %s''',
            (int(horizon_minutes), int(limit)),
            fetch="all",
        )
        out = []
        for row in rows:
            out.append({
                "snapshot_time": str(row.get("snapshot_time")),
                "symbol": row.get("symbol"),
                "snapshot": _loads(row.get("payload_json")) or {},
                "label": _loads(row.get("label_json")) or {},
            })
        return out

    def upsert_v24_feature_snapshot(self, snapshot: dict):
        snapshot_ms = int(snapshot.get("snapshot_ms") or snapshot.get("ts_ms") or 0)
        symbol = str(snapshot.get("symbol") or "")
        if snapshot_ms <= 0 or not symbol:
            raise ValueError("v24 snapshot requires snapshot_ms and symbol")
        params = (
            snapshot_ms,
            symbol,
            float(snapshot.get("best_bid") or 0.0),
            float(snapshot.get("best_ask") or 0.0),
            float(snapshot.get("mid") or 0.0),
            float(snapshot.get("microstructure_score") or 0.0),
            str(snapshot.get("regime") or ""),
            json.dumps(snapshot, default=str),
        )
        self._execute(
            '''INSERT INTO v24_feature_snapshots(
                 snapshot_ms,symbol,best_bid,best_ask,mid,microstructure_score,regime,payload_json
               ) VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol,snapshot_ms) DO UPDATE SET
                 best_bid=excluded.best_bid,best_ask=excluded.best_ask,mid=excluded.mid,
                 microstructure_score=excluded.microstructure_score,regime=excluded.regime,
                 payload_json=excluded.payload_json''',
            '''INSERT INTO v24_feature_snapshots(
                 snapshot_ms,symbol,best_bid,best_ask,mid,microstructure_score,regime,payload_json
               ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(symbol,snapshot_ms) DO UPDATE SET
                 best_bid=EXCLUDED.best_bid,best_ask=EXCLUDED.best_ask,mid=EXCLUDED.mid,
                 microstructure_score=EXCLUDED.microstructure_score,regime=EXCLUDED.regime,
                 payload_json=EXCLUDED.payload_json''',
            params,
        )

    def v24_feature_snapshot_stats(self):
        total = self._execute(
            "SELECT COUNT(*) AS n FROM v24_feature_snapshots",
            "SELECT COUNT(*) AS n FROM v24_feature_snapshots",
            fetch="one",
        )
        symbols = self._execute(
            "SELECT COUNT(DISTINCT symbol) AS n FROM v24_feature_snapshots",
            "SELECT COUNT(DISTINCT symbol) AS n FROM v24_feature_snapshots",
            fetch="one",
        )
        latest = self._execute(
            "SELECT MAX(snapshot_ms) AS ms FROM v24_feature_snapshots",
            "SELECT MAX(snapshot_ms) AS ms FROM v24_feature_snapshots",
            fetch="one",
        )
        return {
            "snapshots": int((total or {}).get("n") or 0),
            "symbols": int((symbols or {}).get("n") or 0),
            "latest_snapshot_ms": None if not latest else latest.get("ms"),
        }

    def insert_v24_price_ticks_batch(self, snapshot_ms: int, features: list[dict]):
        rows = []
        for feature in features or []:
            symbol = str(feature.get("symbol") or "")
            bid = float(feature.get("best_bid") or 0.0)
            ask = float(feature.get("best_ask") or 0.0)
            mid = float(feature.get("mid") or 0.0)
            if not symbol or bid <= 0 or ask <= 0 or mid <= 0:
                continue
            rows.append((int(snapshot_ms), symbol, bid, ask, mid))
        if not rows:
            return 0

        sql_sqlite = '''INSERT INTO v24_price_ticks(snapshot_ms,symbol,best_bid,best_ask,mid)
                        VALUES(?,?,?,?,?)
                        ON CONFLICT(symbol,snapshot_ms) DO UPDATE SET
                          best_bid=excluded.best_bid,best_ask=excluded.best_ask,mid=excluded.mid'''
        sql_pg = '''INSERT INTO v24_price_ticks(snapshot_ms,symbol,best_bid,best_ask,mid)
                    VALUES(%s,%s,%s,%s,%s)
                    ON CONFLICT(symbol,snapshot_ms) DO UPDATE SET
                      best_bid=EXCLUDED.best_bid,best_ask=EXCLUDED.best_ask,mid=EXCLUDED.mid'''
        with self._conn() as conn:
            cur = conn.cursor()
            cur.executemany(sql_pg if self.backend == "postgres" else sql_sqlite, rows)
            if self.backend == "postgres":
                conn.commit()
        return len(rows)

    def v24_price_path(self, symbol: str, start_ms: int, end_ms: int):
        return self._execute(
            '''SELECT snapshot_ms,best_bid,best_ask,mid FROM v24_price_ticks
               WHERE symbol=? AND snapshot_ms>? AND snapshot_ms<=?
               ORDER BY snapshot_ms ASC''',
            '''SELECT snapshot_ms,best_bid,best_ask,mid FROM v24_price_ticks
               WHERE symbol=%s AND snapshot_ms>%s AND snapshot_ms<=%s
               ORDER BY snapshot_ms ASC''',
            (str(symbol), int(start_ms), int(end_ms)),
            fetch="all",
        )

    def v24_price_tick_stats(self):
        total = self._execute(
            "SELECT COUNT(*) AS n FROM v24_price_ticks",
            "SELECT COUNT(*) AS n FROM v24_price_ticks",
            fetch="one",
        )
        latest = self._execute(
            "SELECT MAX(snapshot_ms) AS ms FROM v24_price_ticks",
            "SELECT MAX(snapshot_ms) AS ms FROM v24_price_ticks",
            fetch="one",
        )
        return {
            "ticks": int((total or {}).get("n") or 0),
            "latest_snapshot_ms": None if not latest else latest.get("ms"),
        }

    def prune_v24_price_ticks(self, older_than_ms: int):
        self._execute(
            "DELETE FROM v24_price_ticks WHERE snapshot_ms<?",
            "DELETE FROM v24_price_ticks WHERE snapshot_ms<%s",
            (int(older_than_ms),),
        )

    def clear_v24_feature_labels(self):
        self._execute(
            "DELETE FROM v24_feature_labels",
            "DELETE FROM v24_feature_labels",
        )

    def v24_label_candidates(self, horizon_seconds: int, limit: int = 100, min_snapshot_ms: int = 0):
        cutoff_ms = int(_utc_now().timestamp() * 1000) - int(horizon_seconds) * 1000 - 2000
        return self._execute(
            '''SELECT s.* FROM v24_feature_snapshots s
               LEFT JOIN v24_feature_labels l
                 ON l.symbol=s.symbol AND l.snapshot_ms=s.snapshot_ms
                AND l.horizon_seconds=?
               WHERE s.snapshot_ms>=? AND s.snapshot_ms<=? AND l.symbol IS NULL
               ORDER BY s.snapshot_ms ASC LIMIT ?''',
            '''SELECT s.* FROM v24_feature_snapshots s
               LEFT JOIN v24_feature_labels l
                 ON l.symbol=s.symbol AND l.snapshot_ms=s.snapshot_ms
                AND l.horizon_seconds=%s
               WHERE s.snapshot_ms>=%s AND s.snapshot_ms<=%s AND l.symbol IS NULL
               ORDER BY s.snapshot_ms ASC LIMIT %s''',
            (int(horizon_seconds), int(min_snapshot_ms), cutoff_ms, int(limit)),
            fetch="all",
        )

    def v24_feature_path(self, symbol: str, start_ms: int, end_ms: int):
        return self._execute(
            '''SELECT snapshot_ms,best_bid,best_ask,mid,microstructure_score,payload_json
               FROM v24_feature_snapshots
               WHERE symbol=? AND snapshot_ms>? AND snapshot_ms<=?
               ORDER BY snapshot_ms ASC''',
            '''SELECT snapshot_ms,best_bid,best_ask,mid,microstructure_score,payload_json
               FROM v24_feature_snapshots
               WHERE symbol=%s AND snapshot_ms>%s AND snapshot_ms<=%s
               ORDER BY snapshot_ms ASC''',
            (str(symbol), int(start_ms), int(end_ms)),
            fetch="all",
        )

    def upsert_v24_feature_label(self, label: dict):
        params = (
            str(label["symbol"]),
            int(label["snapshot_ms"]),
            int(label["horizon_seconds"]),
            float(label["entry_ask"]),
            float(label.get("final_bid_return_pct") or 0.0),
            float(label.get("mfe_bid_pct") or 0.0),
            float(label.get("mae_bid_pct") or 0.0),
            bool(label.get("hit_0_1")),
            bool(label.get("hit_0_25")),
            bool(label.get("hit_0_5")),
            bool(label.get("hit_1")),
            bool(label.get("hit_2")),
            json.dumps(label, default=str),
        )
        self._execute(
            '''INSERT INTO v24_feature_labels(
                 symbol,snapshot_ms,horizon_seconds,entry_ask,final_bid_return_pct,mfe_bid_pct,mae_bid_pct,
                 hit_0_1,hit_0_25,hit_0_5,hit_1,hit_2,payload_json,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(symbol,snapshot_ms,horizon_seconds) DO UPDATE SET
                 entry_ask=excluded.entry_ask,final_bid_return_pct=excluded.final_bid_return_pct,
                 mfe_bid_pct=excluded.mfe_bid_pct,mae_bid_pct=excluded.mae_bid_pct,
                 hit_0_1=excluded.hit_0_1,hit_0_25=excluded.hit_0_25,hit_0_5=excluded.hit_0_5,
                 hit_1=excluded.hit_1,hit_2=excluded.hit_2,payload_json=excluded.payload_json,
                 updated_at=CURRENT_TIMESTAMP''',
            '''INSERT INTO v24_feature_labels(
                 symbol,snapshot_ms,horizon_seconds,entry_ask,final_bid_return_pct,mfe_bid_pct,mae_bid_pct,
                 hit_0_1,hit_0_25,hit_0_5,hit_1,hit_2,payload_json,updated_at
               ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
               ON CONFLICT(symbol,snapshot_ms,horizon_seconds) DO UPDATE SET
                 entry_ask=EXCLUDED.entry_ask,final_bid_return_pct=EXCLUDED.final_bid_return_pct,
                 mfe_bid_pct=EXCLUDED.mfe_bid_pct,mae_bid_pct=EXCLUDED.mae_bid_pct,
                 hit_0_1=EXCLUDED.hit_0_1,hit_0_25=EXCLUDED.hit_0_25,hit_0_5=EXCLUDED.hit_0_5,
                 hit_1=EXCLUDED.hit_1,hit_2=EXCLUDED.hit_2,payload_json=EXCLUDED.payload_json,
                 updated_at=NOW()''',
            params,
        )

    def v24_feature_label_stats(self):
        return self._execute(
            '''SELECT horizon_seconds,COUNT(*) AS n,
                      AVG(final_bid_return_pct) AS avg_final,
                      AVG(mfe_bid_pct) AS avg_mfe,
                      AVG(mae_bid_pct) AS avg_mae,
                      AVG(hit_0_1) AS p_hit_0_1,
                      AVG(hit_0_25) AS p_hit_0_25,
                      AVG(hit_0_5) AS p_hit_0_5,
                      AVG(hit_1) AS p_hit_1,
                      AVG(hit_2) AS p_hit_2
               FROM v24_feature_labels GROUP BY horizon_seconds ORDER BY horizon_seconds''',
            '''SELECT horizon_seconds,COUNT(*) AS n,
                      AVG(final_bid_return_pct) AS avg_final,
                      AVG(mfe_bid_pct) AS avg_mfe,
                      AVG(mae_bid_pct) AS avg_mae,
                      AVG((hit_0_1)::int) AS p_hit_0_1,
                      AVG((hit_0_25)::int) AS p_hit_0_25,
                      AVG((hit_0_5)::int) AS p_hit_0_5,
                      AVG((hit_1)::int) AS p_hit_1,
                      AVG((hit_2)::int) AS p_hit_2
               FROM v24_feature_labels GROUP BY horizon_seconds ORDER BY horizon_seconds''',
            fetch="all",
        )

    def v24_labeled_snapshots(self, horizon_seconds: int, limit: int = 20000):
        rows = self._execute(
            '''SELECT s.snapshot_ms,s.symbol,s.payload_json,l.payload_json AS label_json
               FROM v24_feature_snapshots s
               JOIN v24_feature_labels l
                 ON l.symbol=s.symbol AND l.snapshot_ms=s.snapshot_ms
               WHERE l.horizon_seconds=?
               ORDER BY s.snapshot_ms ASC LIMIT ?''',
            '''SELECT s.snapshot_ms,s.symbol,s.payload_json,l.payload_json AS label_json
               FROM v24_feature_snapshots s
               JOIN v24_feature_labels l
                 ON l.symbol=s.symbol AND l.snapshot_ms=s.snapshot_ms
               WHERE l.horizon_seconds=%s
               ORDER BY s.snapshot_ms ASC LIMIT %s''',
            (int(horizon_seconds), int(limit)),
            fetch="all",
        )
        out = []
        for row in rows:
            snapshot = _loads(row.get("payload_json")) or {}
            label = _loads(row.get("label_json")) or {}
            if str(label.get("label_version") or "") != "price_tick_v3_passage":
                continue
            out.append({
                "snapshot_ms": int(row.get("snapshot_ms") or 0),
                "symbol": row.get("symbol"),
                "snapshot": snapshot,
                "label": label,
            })
        return out

    def prune_v24_feature_snapshots(self, older_than_ms: int):
        self._execute(
            "DELETE FROM v24_feature_snapshots WHERE snapshot_ms<?",
            "DELETE FROM v24_feature_snapshots WHERE snapshot_ms<%s",
            (int(older_than_ms),),
        )

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
            (key,), fetch="one",
        )
        if row is None:
            return None
        try:
            value = json.loads(row["value"])
        except Exception:
            value = row["value"]
        return {"value": value, "updated_at": str(row["updated_at"])}

    def worker_health(self, stale_after_seconds: int = 300):
        heartbeat = self.get_runtime("worker_heartbeat")
        error = self.get_runtime("worker_error")
        if heartbeat is None:
            return {"status": "starting", "stale": False, "age_seconds": None, "heartbeat": None, "last_error": error}
        updated = _parse_utc(heartbeat.get("updated_at"))
        age = None if updated is None else max(0.0, (_utc_now() - updated).total_seconds())
        stale = age is None or age > float(stale_after_seconds)
        value = heartbeat.get("value") or {}
        symbols = max(1, int(value.get("symbols") or 0))
        cycle_errors = int(value.get("scan_errors") or 0) + int(value.get("continuation_errors") or 0) + int(value.get("label_errors") or 0)
        severe_cycle_errors = cycle_errors > max(5, int(symbols * 0.10))
        status = "stale" if stale else ("degraded" if severe_cycle_errors else "healthy")
        return {
            "status": status,
            "stale": stale,
            "age_seconds": None if age is None else round(age, 1),
            "cycle_errors": cycle_errors,
            "heartbeat": heartbeat,
            "last_error": error,
        }

    def _research_rows(self):
        return self._execute(
            '''SELECT id,symbol,signal_time,state,continuation_json,derivatives_json,labels_json,last_labeled_horizon
               FROM events ORDER BY id''',
            '''SELECT id,symbol,signal_time,state,continuation_json,derivatives_json,labels_json,last_labeled_horizon
               FROM events ORDER BY id''',
            fetch="all",
        )

    @staticmethod
    def _label_24h(row):
        labels = _loads(row.get("labels_json")) or []
        for label in labels:
            if int(label.get("horizon_minutes") or 0) == 1440:
                return label
        return None

    @staticmethod
    def _cohort_metrics(rows):
        completed = []
        for row in rows:
            label = SignalStore._label_24h(row)
            if label is not None:
                completed.append(label)
        result = {"n_total": len(rows), "n_24h": len(completed)}
        if not completed:
            result.update({
                "p_hit_5": None, "p_hit_10": None, "p_hit_20": None, "p_hit_30": None,
                "median_mfe_24h_pct": None, "median_mae_24h_pct": None,
                "invalidation_rate": None,
            })
            return result
        n = len(completed)
        result.update({
            "p_hit_5": sum(bool(x.get("hit_5_before_invalidation")) for x in completed) / n,
            "p_hit_10": sum(bool(x.get("hit_10_before_invalidation")) for x in completed) / n,
            "p_hit_20": sum(bool(x.get("hit_20_before_invalidation")) for x in completed) / n,
            "p_hit_30": sum(bool(x.get("hit_30_before_invalidation")) for x in completed) / n,
            "median_mfe_24h_pct": statistics.median(float(x.get("mfe_pct") or 0) for x in completed),
            "median_mae_24h_pct": statistics.median(float(x.get("mae_pct") or 0) for x in completed),
            "invalidation_rate": sum(bool(x.get("invalidated")) for x in completed) / n,
        })
        return result

    def research_status(self):
        rows = self._research_rows()
        confirmed, strong, oi_up, oi_2 = [], [], [], []
        pending = 0
        for row in rows:
            if row.get("state") == "IMPULSE":
                pending += 1
            cont = _loads(row.get("continuation_json")) or {}
            deriv = _loads(row.get("derivatives_json")) or {}
            is_confirmed = bool(cont.get("confirmed"))
            if is_confirmed:
                confirmed.append(row)
            if cont.get("tier") == "STRONG":
                strong.append(row)
            oi = deriv.get("oi_change_1h_pct")
            if is_confirmed and oi is not None:
                try:
                    oi_f = float(oi)
                    if oi_f > 0:
                        oi_up.append(row)
                    if oi_f >= 2.0:
                        oi_2.append(row)
                except (TypeError, ValueError):
                    pass

        cohorts = {
            "impulse_all": self._cohort_metrics(rows),
            "continuation_confirmed": self._cohort_metrics(confirmed),
            "continuation_strong": self._cohort_metrics(strong),
            "continuation_plus_oi_up": self._cohort_metrics(oi_up),
            "continuation_plus_oi_2pct": self._cohort_metrics(oi_2),
        }
        imp = cohorts["impulse_all"]
        con = cohorts["continuation_confirmed"]
        oi = cohorts["continuation_plus_oi_up"]
        reasons = []
        if imp["n_24h"] < 100:
            reasons.append("need_at_least_100_completed_shadow_impulses")
        if con["n_24h"] < 30:
            reasons.append("need_at_least_30_completed_confirmed_continuations")
        if oi["n_24h"] < 20:
            reasons.append("need_at_least_20_completed_confirmed_with_oi_up")
        if imp["n_24h"] >= 100 and con["n_24h"] >= 30:
            if (con["p_hit_10"] or 0) <= (imp["p_hit_10"] or 0):
                reasons.append("continuation_does_not_improve_p10")
            if con["invalidation_rate"] is not None and con["invalidation_rate"] > 0.40:
                reasons.append("confirmed_invalidation_rate_above_40pct")

        def ratio(a, b):
            return None if a in (None, 0) or b is None else b / a

        return {
            "generated_at": _utc_now().isoformat(),
            "dataset": {
                "mode": "forward_shadow",
                "total_impulses": len(rows),
                "pending_continuation": pending,
                "confirmed": len(confirmed),
                "strong": len(strong),
                "confirmed_oi_up": len(oi_up),
                "confirmed_oi_2pct": len(oi_2),
                "completed_24h": imp["n_24h"],
            },
            "cohorts": cohorts,
            "uplift": {
                "continuation_p10_multiplier": ratio(imp["p_hit_10"], con["p_hit_10"]),
                "oi_up_vs_continuation_p10_multiplier": ratio(con["p_hit_10"], oi["p_hit_10"]),
            },
            "research_gate": {"passed": len(reasons) == 0, "reasons": reasons},
        }

    def save_daily_snapshot(self, payload=None, snapshot_date=None):
        payload = payload or self.research_status()
        date_value = snapshot_date or _utc_now().date().isoformat()
        raw = json.dumps(payload, default=str)
        self._execute(
            '''INSERT INTO research_snapshots(snapshot_date,payload,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(snapshot_date) DO UPDATE SET payload=excluded.payload,updated_at=CURRENT_TIMESTAMP''',
            '''INSERT INTO research_snapshots(snapshot_date,payload,updated_at) VALUES(%s,%s,NOW())
               ON CONFLICT(snapshot_date) DO UPDATE SET payload=EXCLUDED.payload,updated_at=NOW()''',
            (date_value, raw),
        )
        return date_value

    def confirmed_candidates(self, limit: int = 50):
        rows = self._execute(
            "SELECT * FROM events WHERE continuation_json IS NOT NULL ORDER BY id DESC LIMIT ?",
            "SELECT * FROM events WHERE continuation_json IS NOT NULL ORDER BY id DESC LIMIT %s",
            (max(int(limit) * 5, 100),), fetch="all",
        )
        out = []
        for row in rows:
            cont = _loads(row.get("continuation_json")) or {}
            if not bool(cont.get("confirmed")):
                continue
            out.append({
                "id": row.get("id"),
                "symbol": row.get("symbol"),
                "signal_time": row.get("signal_time"),
                "signal_price": row.get("signal_price"),
                "state": row.get("state"),
                "continuation": cont,
                "derivatives": _loads(row.get("derivatives_json")),
                "readiness": _loads(row.get("readiness_json")),
                "last_labeled_horizon": row.get("last_labeled_horizon"),
            })
            if len(out) >= int(limit):
                break
        return out

    def snapshots(self, limit: int = 30):
        rows = self._execute(
            "SELECT snapshot_date,payload,updated_at FROM research_snapshots ORDER BY snapshot_date DESC LIMIT ?",
            "SELECT snapshot_date,payload,updated_at FROM research_snapshots ORDER BY snapshot_date DESC LIMIT %s",
            (int(limit),), fetch="all",
        )
        out = []
        for row in rows:
            out.append({
                "snapshot_date": str(row["snapshot_date"]),
                "updated_at": str(row["updated_at"]),
                "payload": _loads(row["payload"]),
            })
        return out

    def upsert_historical_event(self, dataset_id, imp, cont, deriv, labels, fold_id):
        params = (
            dataset_id,
            imp.symbol,
            str(imp.signal_time),
            str(imp.available_time),
            float(imp.signal_price),
            json.dumps(asdict(imp), default=str),
            json.dumps(asdict(cont), default=str),
            json.dumps(asdict(deriv), default=str),
            json.dumps(labels, default=str),
            None if fold_id is None else int(fold_id),
        )
        self._execute(
            '''INSERT OR IGNORE INTO historical_events(
                 dataset_id,symbol,signal_time,available_time,signal_price,
                 impulse_json,continuation_json,derivatives_json,labels_json,fold_id
               ) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            '''INSERT INTO historical_events(
                 dataset_id,symbol,signal_time,available_time,signal_price,
                 impulse_json,continuation_json,derivatives_json,labels_json,fold_id
               ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(dataset_id,symbol,signal_time) DO UPDATE SET
                 continuation_json=EXCLUDED.continuation_json,
                 derivatives_json=EXCLUDED.derivatives_json,
                 labels_json=EXCLUDED.labels_json,
                 fold_id=EXCLUDED.fold_id''',
            params,
        )

    def record_historical_symbol_run(
        self, dataset_id, symbol, status, bars15, impulses, oi_rows, funding_rows, error
    ):
        params = (
            dataset_id, symbol, status, int(bars15), int(impulses),
            int(oi_rows), int(funding_rows), error,
        )
        self._execute(
            '''INSERT INTO historical_symbol_runs(
                 dataset_id,symbol,status,bars15,impulses,oi_rows,funding_rows,error,processed_at
               ) VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(dataset_id,symbol) DO UPDATE SET
                 status=excluded.status,bars15=excluded.bars15,impulses=excluded.impulses,
                 oi_rows=excluded.oi_rows,funding_rows=excluded.funding_rows,error=excluded.error,
                 processed_at=CURRENT_TIMESTAMP''',
            '''INSERT INTO historical_symbol_runs(
                 dataset_id,symbol,status,bars15,impulses,oi_rows,funding_rows,error,processed_at
               ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,NOW())
               ON CONFLICT(dataset_id,symbol) DO UPDATE SET
                 status=EXCLUDED.status,bars15=EXCLUDED.bars15,impulses=EXCLUDED.impulses,
                 oi_rows=EXCLUDED.oi_rows,funding_rows=EXCLUDED.funding_rows,error=EXCLUDED.error,
                 processed_at=NOW()''',
            params,
        )

    def _historical_rows(self, dataset_id):
        return self._execute(
            '''SELECT * FROM historical_events WHERE dataset_id=? ORDER BY signal_time''',
            '''SELECT * FROM historical_events WHERE dataset_id=%s ORDER BY signal_time''',
            (dataset_id,), fetch="all",
        )

    def historical_status(self, dataset_id=None):
        state_row = self.get_runtime("historical_backfill_state")
        state = None if state_row is None else state_row.get("value")
        if dataset_id is None:
            dataset_id = None if not state else state.get("dataset_id")
        if not dataset_id:
            return {
                "status": "not_started",
                "dataset_id": None,
                "progress": None,
                "oos": None,
            }

        rows = self._historical_rows(dataset_id)
        oos = [r for r in rows if r.get("fold_id") is not None]
        confirmed = []
        strong = []
        oi_up = []
        oi_2 = []
        for row in oos:
            cont = _loads(row.get("continuation_json")) or {}
            deriv = _loads(row.get("derivatives_json")) or {}
            is_confirmed = bool(cont.get("confirmed"))
            if is_confirmed:
                confirmed.append(row)
            if cont.get("tier") == "STRONG":
                strong.append(row)
            oi = deriv.get("oi_change_1h_pct")
            if is_confirmed and oi is not None:
                try:
                    oi_value = float(oi)
                    if oi_value > 0:
                        oi_up.append(row)
                    if oi_value >= 2.0:
                        oi_2.append(row)
                except (TypeError, ValueError):
                    pass

        cohorts = {
            "impulse_oos": self._cohort_metrics(oos),
            "continuation_confirmed_oos": self._cohort_metrics(confirmed),
            "continuation_strong_oos": self._cohort_metrics(strong),
            "continuation_plus_oi_up_oos": self._cohort_metrics(oi_up),
            "continuation_plus_oi_2pct_oos": self._cohort_metrics(oi_2),
        }

        quality = self._execute(
            '''SELECT status,COUNT(*) AS n FROM historical_symbol_runs
               WHERE dataset_id=? GROUP BY status ORDER BY n DESC''',
            '''SELECT status,COUNT(*) AS n FROM historical_symbol_runs
               WHERE dataset_id=%s GROUP BY status ORDER BY n DESC''',
            (dataset_id,), fetch="all",
        )
        processed = sum(int(r["n"]) for r in quality)
        universe_size = len((state or {}).get("universe") or []) if state and state.get("dataset_id") == dataset_id else None
        cursor = int((state or {}).get("cursor") or 0) if state and state.get("dataset_id") == dataset_id else processed

        imp = cohorts["impulse_oos"]
        con = cohorts["continuation_confirmed_oos"]
        oi = cohorts["continuation_plus_oi_up_oos"]
        reasons = []
        if imp["n_24h"] < 100:
            reasons.append("need_at_least_100_oos_impulses")
        if con["n_24h"] < 30:
            reasons.append("need_at_least_30_oos_confirmed")
        if oi["n_24h"] < 20:
            reasons.append("need_at_least_20_oos_confirmed_with_oi_up")
        if imp["n_24h"] >= 100 and con["n_24h"] >= 30:
            if (con["p_hit_10"] or 0) <= (imp["p_hit_10"] or 0):
                reasons.append("continuation_does_not_improve_p10")
            if con["invalidation_rate"] is not None and con["invalidation_rate"] > 0.40:
                reasons.append("confirmed_invalidation_rate_above_40pct")

        return {
            "status": "complete" if state and state.get("complete") else "running",
            "dataset_id": dataset_id,
            "window": None if not state else {"start": state.get("start"), "end": state.get("end")},
            "progress": {
                "cursor": cursor,
                "universe_size": universe_size,
                "processed_symbols": processed,
                "remaining_symbols": None if universe_size is None else max(0, universe_size - cursor),
                "quality": quality,
                "closed_or_delivered_contracts": None if not state else state.get("closed_or_delivered_contracts"),
                "survivorship_warning": None if not state else state.get("survivorship_warning"),
            },
            "oos": {
                "signals_total": len(oos),
                "confirmed": len(confirmed),
                "strong": len(strong),
                "confirmed_oi_up": len(oi_up),
                "confirmed_oi_2pct": len(oi_2),
                "cohorts": cohorts,
                "research_gate": {"passed": len(reasons) == 0, "reasons": reasons},
            },
        }

    def micro_live_readiness(self):
        hist = self.historical_status()
        reasons = []
        if hist.get("status") == "not_started" or not hist.get("oos"):
            reasons.append("historical_oos_not_available")
            cohorts = {}
        else:
            cohorts = hist["oos"].get("cohorts") or {}
            imp = cohorts.get("impulse_oos") or {}
            con = cohorts.get("continuation_confirmed_oos") or {}
            oi = cohorts.get("continuation_plus_oi_up_oos") or {}

            if int(imp.get("n_24h") or 0) < 50:
                reasons.append("need_50_completed_oos_impulses")
            if int(con.get("n_24h") or 0) < 12:
                reasons.append("need_12_completed_confirmed")
            if int(oi.get("n_24h") or 0) < 8:
                reasons.append("need_8_completed_confirmed_oi_up")

            ip10 = imp.get("p_hit_10")
            cp10 = con.get("p_hit_10")
            if ip10 is None or cp10 is None or cp10 <= ip10:
                reasons.append("continuation_must_improve_p10")
            inv = con.get("invalidation_rate")
            if inv is None or inv > 0.40:
                reasons.append("confirmed_invalidation_must_be_at_most_40pct")

        worker = self.worker_health(int(os.getenv("WORKER_STALE_SECONDS", "300")))
        if worker.get("status") not in ("healthy",):
            reasons.append("worker_not_healthy")

        candidates = self.confirmed_candidates(limit=25)
        now = _utc_now()
        eligible = []
        for c in candidates:
            readiness = c.get("readiness") or {}
            derivatives = c.get("derivatives") or {}
            signal_time = _parse_utc(c.get("signal_time"))
            age_min = None if signal_time is None else (now - signal_time).total_seconds() / 60.0
            if readiness.get("state") != "EARLY ENTRY":
                continue
            if age_min is None or age_min > 90:
                continue
            oi_change = derivatives.get("oi_change_1h_pct")
            funding = derivatives.get("funding_rate")
            if oi_change is None or float(oi_change) < 2.0:
                continue
            if funding is not None and abs(float(funding)) > 0.0015:
                continue
            eligible.append({
                "symbol": c.get("symbol"),
                "signal_time": c.get("signal_time"),
                "signal_price": c.get("signal_price"),
                "state": readiness.get("state"),
                "oi_change_1h_pct": oi_change,
                "funding_rate": funding,
                "age_minutes": round(age_min, 1),
            })

        if not eligible:
            reasons.append("no_fresh_live_early_entry_candidate")

        return {
            "mode": "micro_live_execution_validation",
            "provisional_only": True,
            "full_research_gate_passed": bool(
                (hist.get("oos") or {}).get("research_gate", {}).get("passed", False)
            ) if hist.get("oos") else False,
            "ready": len(reasons) == 0,
            "reasons": reasons,
            "risk_limits": {
                "max_trades_today": 1,
                "max_notional_fraction_of_equity": 0.25,
                "hard_stop_pct": 4.0,
                "target_risk_fraction_of_equity": 0.01,
                "max_daily_loss_usdt": 0.25,
                "max_leverage": 1,
                "averaging": False,
                "martingale": False,
                "require_exchange_min_order_within_risk_budget": True,
            },
            "historical": hist,
            "eligible_candidates": eligible,
        }

    def ping(self):
        with self._conn() as c:
            cur = c.cursor()
            cur.execute("SELECT 1")
            return bool(cur.fetchone())
