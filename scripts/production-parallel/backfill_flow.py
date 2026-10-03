"""Verified two-step Feishu flow for historical backfill dates."""
from __future__ import annotations

import datetime as dt
import json
import re
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
DB = Path.home() / ".openclaw/state/production-parallel/backfill_flow.sqlite"
# Requests and the automatic missing-date queue have their own small state
# database, but a status choice (1/2) is a real production batch.  Its source
# record, review card, confirmation and upload receipt must live in the same
# primary inbox database used by normal reports and the automatic path.
PRIMARY_DB = Path.home() / ".openclaw/state/production-parallel/inbox.sqlite"
WORKERS = {
    "A": ("徐超超", "下机"),
    "B": ("梅芳", "下机"),
    "C": ("李鸿玉", "烤边"),
    "D": ("张小翠", "烤边"),
}
PRODUCTS = ("棉堆堆袜", "冰冰袜", "小腿袜", "过膝袜", "女船袜", "男船袜")
WORKBENCH_VERSION = "V1.15"
CHOICES = {"1": "当天未上班", "2": "已经报过", "3": "需要补报"}


def init(db: str | Path = DB) -> None:
    path = Path(db)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS requests(
            request_key TEXT PRIMARY KEY, worker TEXT NOT NULL, production_date TEXT NOT NULL,
            group_id TEXT NOT NULL, verification_message_id TEXT, template_message_id TEXT,
            status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
        )""")


def register_verification(worker: str, production_date: str, message_id: str,
                          db: str | Path = DB) -> dict:
    """Register an automatic reminder in the same table used by manual cards."""
    init(db)
    day = _day(production_date)
    key = f"{worker}:{day}:{WORKBENCH_VERSION}"
    now = time.time()
    with sqlite3.connect(db) as conn:
        conn.execute("""INSERT INTO requests(
            request_key,worker,production_date,group_id,verification_message_id,
            status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(request_key) DO UPDATE SET
          verification_message_id=excluded.verification_message_id,
          status='verification_sent',updated_at=excluded.updated_at""",
        (key, worker, day, GROUP, str(message_id), 'verification_sent', now, now))
    return {'request_key': key, 'message_id': str(message_id), 'status': 'verification_sent'}


def _day(value: str) -> str:
    return dt.date.fromisoformat(value).isoformat()


def verification_card(worker: str, production_date: str, pending_dates: list[str] | None = None) -> dict:
    day = _day(production_date)
    name, _ = WORKERS[worker]
    content_lines = [
        f"员工：**{worker}｜{name}**",
        f"待核实生产日期：**{day}**",
    ]
    if pending_dates:
        dates = [_day(value) for value in pending_dates]
        content_lines.extend([
            f"待核实日期共 **{len(dates)} 天**：" + "、".join(dates),
            "本次只处理当前日期；当前日期处理完成后，再发送下一张。",
        ])
    content_lines.extend([
        "",
        "系统暂未查到这一天的有效生产记录，请选择实际情况：",
        "",
        "1. 当天未上班",
        "2. 已经报过",
        "3. 需要补报",
        "",
        "请点击下面对应按钮；如果按钮暂时无法使用，请回复对应数字或文字。",
    ])
    content = "\n".join(content_lines)
    actions = []
    for number, label in CHOICES.items():
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": f"{number} {label}"},
            # Keep all choices visually neutral; blue implies a preferred
            # answer and makes the employee think option 3 is preselected.
            "type": "default",
            "value": {"text": f"生产补报 {number} {worker} {day}"},
        })
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "补发生产日期待核实"}, "template": "orange"},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": content}},
            {"tag": "action", "actions": actions},
        ],
    }


def template_text(worker: str, production_date: str) -> str:
    day = _day(production_date)
    name, process = WORKERS[worker]
    return "\n".join([
        "补报生产数据",
        f"员工：{worker}｜{name}",
        f"生产日期：{day}",
        f"工序：{process}",
        "",
        "请在下面六个产品名称后的冒号后直接填写数字，没有生产填0，然后整段发回：",
        "补报",
        f"{worker}={name}",
        f"生产日期：{day}",
        *[f"{product}：" for product in PRODUCTS],
        "",
        "六项必须全部保留；只填写数字，不修改员工、日期、工序和产品名称。",
        "填完后发送整段内容，系统会生成核对卡，回复“准确”后才上传。",
        f"当天未上班请回复：未上班：{day}",
        f"已经报过请回复：已报待查：{day}",
    ])


def template_card(worker: str, production_date: str) -> dict:
    """Build the same canonical card used by automatic reminders."""
    from card_builder import backfill_input_payload
    return backfill_input_payload(worker, _day(production_date))


def handle_template(message_id: str, sender: str, content: str,
                    db: str | Path = DB) -> dict | None:
    """Convert a returned six-field backfill template into one review card.

    This bypasses the ordinary report acknowledgement path.  The ordinary
    parser used to acknowledge a template and wait for the model queue, which
    left the employee without a review card.  Owner-side tests are allowed,
    but the resulting batch remains bound to the target employee account.
    """
    text = str(content or "").strip()
    if "补报" not in text or "生产日期" not in text or not any(p in text for p in PRODUCTS):
        return None
    day_match = re.search(r"生产日期\s*[：:=]\s*(20\d{2}-\d{2}-\d{2})", text)
    worker_match = re.search(r"(?m)^\s*([ABCD])\s*[=:：]\s*", text)
    if not day_match or not worker_match:
        return None
    worker, day = worker_match.group(1), _day(day_match.group(1))
    values: dict[str, int] = {}
    missing: list[str] = []
    for product in PRODUCTS:
        match = re.search(rf"{re.escape(product)}\s*[：:=]\s*([0-9][0-9,]*)\s*(?:双)?", text)
        if not match:
            missing.append(product)
        else:
            values[product] = int(match.group(1).replace(",", ""))
    if missing:
        return {"handled": True,
                "text": f"已收到补报模板，但以下项目还没有填写：{'、'.join(missing)}。没有生产的项目请明确填写 0，六项完整后再整段发送。"}
    import hashlib
    import queue_store as q
    import review_cards
    import worker_identity
    init(db)
    request_db = Path(db).resolve()
    queue_db = str(PRIMARY_DB if request_db == DB.resolve() else request_db)
    with q.conn(queue_db) as conn:
        identity = conn.execute("SELECT platform FROM worker_identity WHERE worker=?", (worker,)).fetchone()
    target_sender = identity["platform"] if identity else sender
    if target_sender != sender and sender != review_cards.OWNER:
        return {"handled": True, "text": "补报模板已收到，但发送账号与梅芳的已核实账号不一致，请由员工本人发送。"}
    source = str(message_id or "")
    if not re.fullmatch(r"om_[A-Za-z0-9_-]+", source):
        return {"handled": True, "text": "补报模板消息凭证无效，请重新整段发送一次。"}
    with q.conn(queue_db) as conn:
        existing = conn.execute("SELECT message_id FROM review_cards WHERE source=? AND state IN ('pending','confirmed') ORDER BY rowid DESC LIMIT 1", (source,)).fetchone()
    if existing and existing["message_id"]:
        return {"handled": True, "text": "这份补报数据已经生成核对卡，请回复“准确”或“错误”。", "message_id": existing["message_id"]}
    event = {"messageId": source, "senderId": target_sender, "groupId": GROUP, "content": text}
    result = {"agent": "补报模板规则化解析", "draft_only": True,
              "extracted": {"kind": "report", "worker": worker, "production_date": day,
                            "items": [{"product": p, "process": WORKERS[worker][1], "quantity": values[p]} for p in PRODUCTS],
                            "missing": [], "backfill": True,
                            "notes": ["补报模板六项完整，等待员工回复准确"]}}
    raw = json.dumps(event, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(json.dumps({k: event[k] for k in ('messageId', 'senderId', 'groupId', 'content')}, sort_keys=True).encode()).hexdigest()
    q.init(queue_db)
    with q.conn(queue_db) as conn:
        conn.execute("INSERT OR IGNORE INTO inbox(id,sender,grp,event,digest,status,result,created) VALUES(?,?,?,?,?,?,?,?)",
                     (source, target_sender, GROUP, raw, digest, "ready", json.dumps(result, ensure_ascii=False), time.time()))
    summary = (f"身份：{worker}={WORKERS[worker][0]} 请核实\n生产日期：{day}\n工序：{WORKERS[worker][1]}\n" +
               "".join(f"{p}：{values[p]}双\n" for p in PRODUCTS) +
               f"合计：{sum(values.values())}双\n记录类型：补报")
    issued = review_cards.issue(queue_db, source, summary)
    delivered = review_cards.deliver_card(queue_db, issued["token"])
    if delivered.get("status") != "sent":
        raise RuntimeError("补报核对卡发送失败")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE requests SET template_message_id=?,status='template_review_sent',updated_at=? WHERE worker=? AND production_date=? AND request_key LIKE '%:V1.%'",
                     (delivered.get("messageId"), time.time(), worker, day))
    return {"handled": True, "text": "已生成生产补报数核对卡，请核对六项数量后回复“准确”。", "message_id": delivered.get("messageId")}


def _send(body: dict, request_id: str) -> str:
    from transport import request
    result = request("xiaowen", "POST", "/im/v1/messages?receive_id_type=chat_id", {
        "receive_id": GROUP,
        "msg_type": "interactive",
        "content": json.dumps(body, ensure_ascii=False),
        "uuid": request_id,
    })
    message_id = (result.get("data") or {}).get("message_id")
    if not message_id:
        raise RuntimeError("飞书未返回消息ID")
    return str(message_id)


def _send_text(content: str, request_id: str) -> str:
    from transport import request
    result = request("xiaowen", "POST", "/im/v1/messages?receive_id_type=chat_id", {
        "receive_id": GROUP,
        "msg_type": "text",
        "content": json.dumps({"text": content}, ensure_ascii=False),
        "uuid": request_id,
    })
    message_id = (result.get("data") or {}).get("message_id")
    if not message_id:
        raise RuntimeError("飞书未返回文字模板消息ID")
    return str(message_id)


def send_verification(worker: str, production_date: str, db: str | Path = DB) -> dict:
    init(db)
    day = _day(production_date)
    key = f"{worker}:{day}:{WORKBENCH_VERSION}"
    retracted = False
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM requests WHERE request_key=?", (key,)).fetchone()
        if row and row[4] and not _message_is_deleted(row[4]):
            return {"request_key": key, "status": "already_sent", "message_id": row[4]}
        if row and row[4] and _message_is_deleted(row[4]):
            retracted = True
            conn.execute("UPDATE requests SET verification_message_id=NULL,template_message_id=NULL,status='verification_sent',updated_at=? WHERE request_key=?", (time.time(), key))
        now = time.time()
        conn.execute("""INSERT INTO requests(
            request_key,worker,production_date,group_id,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?) ON CONFLICT(request_key) DO UPDATE SET status='sending',updated_at=?""",
        (key, worker, day, GROUP, "sending", now, now, now))
        pending_rows = conn.execute(
            "SELECT production_date FROM requests WHERE worker=? "
            "AND production_date>=? AND status IN "
            "('sending','verification_sent','queued_after_previous','template_sent',"
            "'not_worked_review_sent','already_reported_review_sent') "
            "ORDER BY production_date",
            (worker, day),
        ).fetchall()
    pending_dates = [item['production_date'] for item in pending_rows]
    request_id = str(uuid.uuid4()) if retracted else str(uuid.uuid5(uuid.NAMESPACE_URL, f"verify:{GROUP}:{key}"))
    mid = _send(verification_card(worker, day, pending_dates), request_id)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE requests SET verification_message_id=?,status='verification_sent',updated_at=? WHERE request_key=?",
                     (mid, time.time(), key))
    return {"request_key": key, "status": "sent", "message_id": mid}


def _parent_message(message_id: str) -> str | None:
    # Visible-button fallbacks may carry a synthetic callback id rather than
    # a Feishu message id.  Skip the network lookup in that case.
    if not str(message_id or '').startswith('om_'):
        return None
    from transport import request
    try:
        result = request("xiaowen", "GET", f"/im/v1/messages/{message_id}")
        item = (result.get("data") or {}).get("items", [{}])[0]
        return item.get("parent_id") or item.get("root_id")
    except Exception:
        # A button fallback may arrive with a synthetic/non-message callback
        # id.  The caller will use the newest active V1.15 request instead of
        # handing the text to the generic chat model.
        return None


def _message_is_deleted(message_id: str) -> bool:
    """Return whether a previously recorded Feishu card was withdrawn."""
    if not message_id:
        return False
    try:
        from transport import request
        item = request("xiaowen", "GET", f"/im/v1/messages/{message_id}")["data"]["items"][0]
        return bool(item.get("deleted"))
    except Exception:
        # An unavailable lookup must not silently create a duplicate. Keep the
        # existing idempotent state and let the operator retry after recovery.
        return False


def _find_request(message_id: str, worker: str | None, day: str | None, db: str | Path) -> tuple | None:
    init(db)
    with sqlite3.connect(db) as conn:
        if message_id:
            row = conn.execute("SELECT * FROM requests WHERE verification_message_id=?", (message_id,)).fetchone()
            if row:
                return row
        if worker and day:
            return conn.execute("SELECT * FROM requests WHERE worker=? AND production_date=? ORDER BY created_at DESC LIMIT 1",
                                (worker, _day(day))).fetchone()
        # Feishu clients do not all preserve the card callback payload.  Some
        # send only the visible button text (or a bare 1/2/3), with no parent
        # message id.  Resolve that fallback to the single newest active V1.15
        # verification/template request rather than letting the generic model
        # answer the button as a normal chat message.
        return conn.execute(
            "SELECT * FROM requests WHERE request_key LIKE '%:V1.%' "
            "AND status IN ('verification_sent','template_sent',"
            "'template_send_failed','not_worked_review_sent',"
            "'already_reported_review_sent') ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    return None


def handle_choice(message_id: str, sender: str, content: str, db: str | Path = DB) -> dict | None:
    """Handle a numeric/text choice replied to a verification card."""
    text = str(content or "").strip()
    m = re.fullmatch(r"生产补报\s+([1-3])\s+([ABCD])\s+(20\d{2}-\d{2}-\d{2})", text)
    number = m.group(1) if m else None
    worker = m.group(2) if m else None
    day = m.group(3) if m else None
    if not m:
        number = {
            "1": "1", "1 当天未上班": "1", "当天未上班": "1",
            "2": "2", "2 已经报过": "2", "已经报过": "2",
            "3": "3", "3 需要补报": "3", "补报": "3", "需要补报": "3",
        }.get(text)
        if number:
            parent = _parent_message(message_id)
            row = _find_request(parent, None, None, db)
        else:
            return None
    else:
        row = _find_request(None, worker, day, db)
    if not row:
        return {"handled": True, "text": "没有找到对应的生产日期核实卡，请回复对应日期的核实卡。"}
    worker, day = row[1], row[2]
    if row[6] in {"uploaded", "superseded"}:
        return {"handled": True, "text": f"{day} 已完成处理，旧核实卡不会重复上传。"}
    if row[6] in {"not_worked_review_sent", "already_reported_review_sent"} and row[5]:
        if not _message_is_deleted(row[5]):
            return {"handled": True, "text": f"已生成{CHOICES[number]}核对卡，请回复“准确”后同步 GitHub。", "message_id": row[5]}
    if number == "3":
        mid = row[5]
        # Only reuse a live template for a request explicitly in template_sent
        # state. A withdrawn/retracted card may leave its old message id in the
        # audit row; that stale id must never suppress a fresh template.
        if mid and row[6] == "template_sent":
            if not _message_is_deleted(mid):
                return {"handled": True, "text": "已发送对应日期的补报模板，请只修改六个数量数字。"}
            with sqlite3.connect(db) as conn:
                conn.execute("UPDATE requests SET template_message_id=NULL,status='verification_sent',updated_at=? WHERE request_key=?",
                             (time.time(), row[0]))
        # A withdrawn card cannot be recovered with the old deterministic
        # request UUID. Use a fresh UUID for this new delivery cycle.
        try:
            template_mid = _send(template_card(worker, day), str(uuid.uuid4()))
        except Exception as exc:
            with sqlite3.connect(db) as conn:
                conn.execute("UPDATE requests SET status='template_send_failed',updated_at=? WHERE request_key=?",
                             (time.time(), row[0]))
            return {"handled": True,
                    "text": f"{day} 的补报模板暂未发送，请稍后重试；日期状态已保留，不会重复入账。",
                    "error": str(exc)}
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE requests SET template_message_id=?,status='template_sent',updated_at=? WHERE request_key=?",
                         (template_mid, time.time(), row[0]))
        return {"handled": True, "text": "已发送对应日期的补报模板，请只修改六个数量数字。", "message_id": template_mid}
    status = {"1": "not_worked", "2": "already_reported"}[number]
    try:
        review = _create_status_review(worker, day, sender, status, db)
    except Exception as exc:
        # Keep the choice auditable, but do not claim that a follow-up card
        # exists when Feishu delivery or identity binding failed.
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE requests SET status=?,updated_at=? WHERE request_key=?",
                         (status + '_review_failed', time.time(), row[0]))
        return {"handled": True,
                "text": f"已记录：{CHOICES[number]}（{day}），但核对卡暂未发送，请稍后重试。",
                "error": str(exc)}
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE requests SET status=?,template_message_id=?,updated_at=? WHERE request_key=?",
                     (status + '_review_sent', review.get('message_id'), time.time(), row[0]))
    return {"handled": True, "text": f"已记录：{CHOICES[number]}（{day}），已生成核对卡；回复“准确”后同步 GitHub。", "message_id": review.get('message_id')}


def _create_status_review(worker: str, day: str, sender: str, status: str,
                          db: str | Path = DB) -> dict:
    """Turn choices 1/2 into a status-only review/upload batch."""
    import hashlib
    import queue_store as q
    import review_cards
    init(db)
    # Native card callbacks can be clicked by the owner while testing or while
    # correcting an employee's historical date.  The review card must still be
    # bound to the target employee's verified Feishu account so that the
    # employee's later “准确” is accepted.  Only the owner may act as this
    # proxy; an unrelated account is rejected by the normal identity guard.
    target_sender = sender
    with q.conn(str(PRIMARY_DB if Path(db).resolve() == DB.resolve() else Path(db).resolve())) as conn:
        identity = conn.execute('SELECT platform FROM worker_identity WHERE worker=?', (worker,)).fetchone()
    if identity:
        target_sender = identity['platform'] if hasattr(identity, 'keys') else identity[0]
    if target_sender != sender:
        import review_cards as _review_cards
        if sender != _review_cards.OWNER:
            raise RuntimeError('点击账号与目标员工身份不匹配')
    # upload_task validates source IDs as Feishu-style ``om_`` IDs.  Keep the
    # synthetic status batch deterministic and bind its identity to the target
    # employee, never to the account that happened to click the card.  This
    # prevents a prior owner test source from poisoning the employee batch.
    source = f"om_backfill_status_{worker}_{day}_{hashlib.sha256((target_sender + status).encode()).hexdigest()[:16]}"
    content = (f"{status}：{day}\n{worker}={WORKERS[worker][0]}\n"
               f"生产日期：{day}\n工序：{WORKERS[worker][1]}\n" +
               ''.join(f"{product}：0\n" for product in PRODUCTS))
    event = {'messageId': source, 'senderId': target_sender, 'groupId': GROUP, 'content': content}
    result = {'agent': '补报状态规则化处理', 'draft_only': True,
              'extracted': {'kind': 'report', 'worker': worker,
                            'production_date': day,
                            'items': [{'product': p, 'process': WORKERS[worker][1], 'quantity': 0}
                                      for p in PRODUCTS],
                            'missing': [], 'status_only': True,
                            'attendance_status': status,
                            'not_worked': status == 'not_worked',
                            'already_reported': status == 'already_reported',
                            'notes': [
                                '当天未上班' if status == 'not_worked' else '已经报过',
                                '该批次仍须员工回复准确后上传 GitHub'],
                            'backfill': False}}
    raw = json.dumps(event, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(json.dumps({k: event[k] for k in ('messageId', 'senderId', 'groupId', 'content')}, sort_keys=True).encode()).hexdigest()
    # Production calls use the primary inbox DB.  Temporary unit-test DBs
    # continue to stay isolated when a caller passes an explicit alternate
    # path, so the test suite never touches the live queue.
    request_db = Path(db).resolve()
    queue_db = str(PRIMARY_DB if request_db == DB.resolve() else request_db)
    q.init(queue_db)
    with q.conn(queue_db) as conn:
        conn.execute('''INSERT OR IGNORE INTO inbox(id,sender,grp,event,digest,status,result,created)
                        VALUES(?,?,?,?,?,?,?,?)''',
                     (source, target_sender, GROUP, raw, digest, 'ready', json.dumps(result, ensure_ascii=False), time.time()))
    status_label = '当天未上班' if status == 'not_worked' else '已经报过'
    summary = (f"身份：{worker}={WORKERS[worker][0]} 请核实\n"
               f"生产日期：{day}\n"
               f"出勤状态：{status_label}\n"
               "该日期不生成生产数量，不计入生产累计；请核实此状态。")
    issued = review_cards.issue(queue_db, source, summary)
    delivered = review_cards.deliver_card(queue_db, issued['token'])
    if delivered.get('status') != 'sent':
        raise RuntimeError('状态核对卡发送失败')
    return {'message_id': delivered.get('messageId'), 'source': source, 'token': issued['token']}


def advance_after_upload(worker: str, completed_date: str, db: str | Path = DB,
                         receipt_message_id: str | None = None) -> dict:
    """Mark one backfill date complete and deliver only the next queued date.

    This is called only after the normal upload worker has a verified GitHub
    commit and remote readback.  A missing next row is a normal terminal state;
    no bulk delivery is attempted.
    """
    if worker not in WORKERS:
        raise ValueError("未知员工代号")
    completed = _day(completed_date)
    init(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE requests SET status='uploaded',updated_at=? "
            "WHERE worker=? AND production_date=? AND status IN "
            "('verification_sent','template_sent','not_worked','already_reported',"
            "'not_worked_review_sent','already_reported_review_sent','template_review_sent')",
            (time.time(), worker, completed),
        )
        row = conn.execute(
            "SELECT production_date FROM requests WHERE worker=? "
            "AND production_date>? AND status='queued_after_previous' "
            "AND request_key LIKE '%:V1.%' "
            "ORDER BY production_date LIMIT 1",
            (worker, completed),
        ).fetchone()
    try:
        import missing_alerts
        missing_alerts.mark_receipt_confirmed(worker, completed, receipt_message_id)
    except Exception:
        pass
    if not row:
        return {"worker": worker, "completed_date": completed, "status": "complete", "next": None}
    next_day = row[0]
    # Automatic reminders own the next date when one is already queued.  Do
    # not send a second manual card for the same worker/date.
    try:
        import missing_alerts
        if missing_alerts.has_pending_alert(worker, next_day):
            return {"worker": worker, "completed_date": completed, "status": "advanced_queued",
                    "next": next_day, "delivery": {"status": "waiting_for_automatic_queue"}}
    except Exception:
        pass
    result = send_verification(worker, next_day, db=db)
    return {"worker": worker, "completed_date": completed, "status": "advanced",
            "next": next_day, "delivery": result}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["send-verification", "choice", "template", "advance"])
    parser.add_argument("--worker")
    parser.add_argument("--date")
    parser.add_argument("--message-id")
    parser.add_argument("--sender", default="")
    parser.add_argument("--text", default="")
    parser.add_argument("--completed-date")
    parser.add_argument("--receipt-message-id")
    args = parser.parse_args()
    if args.command == "send-verification":
        print(json.dumps(send_verification(args.worker, args.date), ensure_ascii=False))
    elif args.command == "choice":
        print(json.dumps(handle_choice(args.message_id, args.sender, args.text), ensure_ascii=False))
    elif args.command == "template":
        print(json.dumps(handle_template(args.message_id, args.sender, args.text), ensure_ascii=False))
    else:
        if not args.worker or not args.completed_date:
            parser.error("advance requires --worker and --completed-date")
        print(json.dumps(advance_after_upload(args.worker, args.completed_date,
                                              receipt_message_id=args.receipt_message_id), ensure_ascii=False))
