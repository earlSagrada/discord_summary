"""SQLite 持久化：信号→结果 的长期台账（对应设计文档 §6 回测）。

单文件 data/signals.db。表：
  runs      每次跑 signals.py 一条
  mentions  该次抽到的标的 + 提及次数
  signals   每个标的的打分结果（含建议入场/止损/价位快照）
  outcomes  预留：T+1/3/5 回填走势（B4 做回测时写）
market_cache/ 是可丢弃的行情缓存，不进这里。
"""

import json
import sqlite3
from datetime import datetime, timezone

import config

DB_PATH = config.DATA_DIR / "signals.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    source  TEXT
);
CREATE TABLE IF NOT EXISTS mentions (
    run_id  INTEGER NOT NULL,
    ticker  TEXT NOT NULL,
    type    TEXT,
    count   INTEGER,
    samples TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS signals (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    INTEGER NOT NULL,
    ts        TEXT NOT NULL,
    ticker    TEXT NOT NULL,
    type      TEXT,
    tier      TEXT,
    light     TEXT,
    signals   TEXT,
    price     REAL,
    entry     REAL,
    stop      REAL,
    checklist TEXT,
    notes     TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS outcomes (
    signal_id INTEGER NOT NULL,
    horizon   TEXT NOT NULL,
    price     REAL,
    ret_pct   REAL,
    filled_ts TEXT,
    PRIMARY KEY (signal_id, horizon),
    FOREIGN KEY (signal_id) REFERENCES signals(id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(_SCHEMA)
    return conn


def new_run(conn: sqlite3.Connection, source: str) -> int:
    cur = conn.execute("INSERT INTO runs (ts, source) VALUES (?, ?)", (_now(), source))
    conn.commit()
    return int(cur.lastrowid)


def add_mention(conn: sqlite3.Connection, run_id: int, m: dict) -> None:
    conn.execute(
        "INSERT INTO mentions (run_id, ticker, type, count, samples) VALUES (?,?,?,?,?)",
        (run_id, m["ticker"], m["type"], m["count"], json.dumps(m["samples"], ensure_ascii=False)),
    )


def add_signal(conn: sqlite3.Connection, run_id: int, card: dict) -> int:
    cur = conn.execute(
        """INSERT INTO signals
           (run_id, ts, ticker, type, tier, light, signals, price, entry, stop, checklist, notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            run_id, _now(), card["ticker"], card["type"], card["tier"], card["light"],
            json.dumps(card["signals"], ensure_ascii=False), card.get("price"),
            card.get("entry"), card.get("stop"),
            json.dumps(card["checklist"], ensure_ascii=False),
            card.get("notes", ""),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)
