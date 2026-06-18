"""Database layer — SQLite for request logging & key management"""
import sqlite3, time, threading
from pathlib import Path

DB = Path(__file__).parent.parent / "prem2api.db"
_local = threading.local()


def _get():
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(str(DB))
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
    return _local.conn


def init():
    conn = _get()
    # 兼容旧表: 加 ttft_ms 列(如不存在)
    try:
        conn.execute("ALTER TABLE requests ADD COLUMN ttft_ms INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS requests (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            model       TEXT NOT NULL,
            channel     TEXT NOT NULL,
            status      INTEGER NOT NULL,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            latency_ms  INTEGER DEFAULT 0,
            ttft_ms     INTEGER DEFAULT 0,
            client_ip   TEXT DEFAULT '',
            timestamp   INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_req_time ON requests(timestamp);
        CREATE INDEX IF NOT EXISTS idx_req_model ON requests(model, timestamp);

        CREATE TABLE IF NOT EXISTS channels_meta (
            name        TEXT PRIMARY KEY,
            total_keys  INTEGER DEFAULT 0,
            alive_keys  INTEGER DEFAULT 0,
            updated_at  INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS admin_keys (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            key_value   TEXT NOT NULL UNIQUE,
            label       TEXT DEFAULT '',
            is_active   INTEGER DEFAULT 1,
            created_at  INTEGER NOT NULL
        );
    """)
    conn.commit()


LOG_RETENTION_HOURS = 48


def _cleanup():
    """删除超过 48h 的旧日志"""
    conn = _get()
    cutoff = int(time.time()) - LOG_RETENTION_HOURS * 3600
    conn.execute("DELETE FROM requests WHERE timestamp < ?", (cutoff,))
    conn.commit()


def log_request(model, channel, status, prompt_tokens=0,
                completion_tokens=0, latency_ms=0, ttft_ms=0, client_ip=""):
    conn = _get()
    conn.execute(
        "INSERT INTO requests(model,channel,status,prompt_tokens,completion_tokens,latency_ms,ttft_ms,client_ip,timestamp) VALUES(?,?,?,?,?,?,?,?,?)",
        (model, channel, status, prompt_tokens, completion_tokens,
         latency_ms, ttft_ms, client_ip, int(time.time())),
    )
    conn.commit()


def update_channel_stats(name, total, alive):
    conn = _get()
    conn.execute(
        "INSERT OR REPLACE INTO channels_meta(name,total_keys,alive_keys,updated_at) VALUES(?,?,?,?)",
        (name, total, alive, int(time.time())),
    )
    conn.commit()


# ---- Stats ----

def stats_summary(hours=24):
    conn = _get()
    since = int(time.time()) - hours * 3600
    row = conn.execute("""
        SELECT
            COUNT(*) AS total_requests,
            SUM(CASE WHEN status=200 THEN 1 ELSE 0 END) AS success,
            SUM(CASE WHEN status!=200 THEN 1 ELSE 0 END) AS failed,
            COALESCE(SUM(prompt_tokens),0) AS total_prompt,
            COALESCE(SUM(completion_tokens),0) AS total_completion,
            COALESCE(AVG(CASE WHEN status=200 THEN latency_ms END),0) AS avg_latency,
            COALESCE(AVG(CASE WHEN status=200 THEN ttft_ms END),0) AS avg_ttft
        FROM requests WHERE timestamp >= ?
    """, (since,)).fetchone()
    return dict(row)


def stats_by_model(hours=24):
    conn = _get()
    since = int(time.time()) - hours * 3600
    rows = conn.execute("""
        SELECT model, COUNT(*) AS reqs,
               SUM(CASE WHEN status=200 THEN 1 ELSE 0 END) AS success,
               SUM(prompt_tokens+completion_tokens) AS total_tokens,
               AVG(latency_ms) AS avg_latency,
               AVG(ttft_ms) AS avg_ttft
        FROM requests WHERE timestamp >= ?
        GROUP BY model ORDER BY reqs DESC
    """, (since,)).fetchall()
    return [dict(r) for r in rows]


def stats_by_channel(hours=24):
    conn = _get()
    since = int(time.time()) - hours * 3600
    rows = conn.execute("""
        SELECT channel, COUNT(*) AS reqs,
               SUM(CASE WHEN status=200 THEN 1 ELSE 0 END) AS success,
               SUM(prompt_tokens+completion_tokens) AS total_tokens
        FROM requests WHERE timestamp >= ?
        GROUP BY channel ORDER BY reqs DESC
    """, (since,)).fetchall()
    return [dict(r) for r in rows]


def stats_timeline(hours=24):
    conn = _get()
    since = int(time.time()) - hours * 3600
    rows = conn.execute("""
        SELECT timestamp/600*600 AS bucket,
               COUNT(*) AS reqs,
               SUM(prompt_tokens+completion_tokens) AS tokens
        FROM requests WHERE timestamp >= ?
        GROUP BY bucket ORDER BY bucket
    """, (since,)).fetchall()
    return [{"time": r["bucket"], "reqs": r["reqs"], "tokens": r["tokens"]}
            for r in rows]


def get_logs(limit=100, offset=0, model=None, status=None):
    conn = _get()
    where = []
    params = []
    if model:
        where.append("model=?")
        params.append(model)
    if status is not None:
        where.append("status=?")
        params.append(status)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT * FROM requests {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    return [dict(r) for r in rows]


# ---- Admin keys ----

def add_admin_key(key_value, label=""):
    conn = _get()
    conn.execute(
        "INSERT OR IGNORE INTO admin_keys(key_value,label,created_at) VALUES(?,?,?)",
        (key_value, label, int(time.time())),
    )
    conn.commit()


def list_admin_keys():
    conn = _get()
    rows = conn.execute(
        "SELECT * FROM admin_keys ORDER BY id DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def toggle_admin_key(key_id, active):
    conn = _get()
    conn.execute("UPDATE admin_keys SET is_active=? WHERE id=?", (1 if active else 0, key_id))
    conn.commit()


def delete_admin_key(key_id):
    conn = _get()
    conn.execute("DELETE FROM admin_keys WHERE id=?", (key_id,))
    conn.commit()


def verify_admin_key(key_value):
    conn = _get()
    row = conn.execute(
        "SELECT 1 FROM admin_keys WHERE key_value=? AND is_active=1",
        (key_value,),
    ).fetchone()
    return row is not None
