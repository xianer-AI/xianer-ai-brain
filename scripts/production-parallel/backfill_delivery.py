"""Idempotently send one real Feishu backfill-input card per production date."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPANION = HERE.parent / "group-companion"
if str(COMPANION) not in sys.path:
    sys.path.insert(0, str(COMPANION))

GROUP = "oc_1f8587b1bcde12a0d1bb6053ab2b748a"
STATE_DB = Path.home() / ".openclaw/state/production-parallel/backfill_alerts.sqlite"
WORKERS = {
    "A": ("徐超超", "下机"),
    "B": ("梅芳", "下机"),
    "C": ("李鸿玉", "烤边"),
    "D": ("张小翠", "烤边"),
}
PRODUCTS = ("棉堆堆袜", "冰冰袜", "小腿袜", "过膝袜", "女船袜", "男船袜")


def _init(db: str | Path) -> None:
    path = Path(db)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS backfill_alerts(
            alert_key TEXT PRIMARY KEY, worker TEXT NOT NULL, production_date TEXT NOT NULL,
            group_id TEXT NOT NULL, status TEXT NOT NULL, message_id TEXT,
            sent_at REAL, error TEXT
        )""")


def card(worker: str, production_date: str) -> dict:
    from card_builder import backfill_input_payload
    return backfill_input_payload(worker, dt.date.fromisoformat(production_date).isoformat())


def deliver(db: str | Path, worker: str, production_date: str, send=None) -> dict:
    """Send once and retain the Feishu receipt for duplicate suppression."""
    _init(db)
    day = dt.date.fromisoformat(production_date).isoformat()
    key = f"{worker}:{day}:V1.15"
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        old = conn.execute(
            "SELECT status,message_id FROM backfill_alerts WHERE alert_key=?", (key,)
        ).fetchone()
        if old and old["status"] == "sent" and old["message_id"]:
            return {"alert_key": key, "status": "already_sent", "message_id": old["message_id"]}
        conn.execute("""INSERT INTO backfill_alerts(
            alert_key,worker,production_date,group_id,status,message_id,sent_at,error
        ) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(alert_key) DO UPDATE SET status='sending',error=NULL""",
        (key, worker, day, GROUP, "sending", None, None, None))
    payload = card(worker, day)
    token = str(uuid.uuid5(uuid.NAMESPACE_URL, f"feishu:{GROUP}:backfill:{key}"))
    if send is None:
        from transport import request
        def send(body, request_id):
            result = request("xiaowen", "POST", "/im/v1/messages?receive_id_type=chat_id", {
                "receive_id": GROUP,
                "msg_type": "interactive",
                "content": json.dumps(body, ensure_ascii=False),
                "uuid": request_id,
            })
            return (result.get("data") or {}).get("message_id")
    try:
        message_id = send(payload, token)
        if not message_id:
            raise RuntimeError("飞书未返回消息ID")
    except Exception as exc:
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE backfill_alerts SET status='failed',error=? WHERE alert_key=?",
                         (type(exc).__name__ + ": " + str(exc)[:180], key))
        raise
    with sqlite3.connect(db) as conn:
        conn.execute("""UPDATE backfill_alerts SET status='sent',message_id=?,sent_at=?,error=NULL
                        WHERE alert_key=?""", (str(message_id), time.time(), key))
    return {"alert_key": key, "status": "sent", "message_id": str(message_id)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("worker", choices=sorted(WORKERS))
    parser.add_argument("dates", nargs="+")
    parser.add_argument("--db", default=str(STATE_DB))
    args = parser.parse_args()
    print(json.dumps(
        [deliver(args.db, args.worker, value) for value in args.dates],
        ensure_ascii=False,
    ))


if __name__ == "__main__":
    main()
