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

    def ping(self):
        with self._conn() as c:
            cur = c.cursor()
            cur.execute("SELECT 1")
            return bool(cur.fetchone())
