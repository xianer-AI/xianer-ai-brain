"""Build idempotent employee reminders for fixed-rota coverage gaps.

This module is deliberately transport-free.  The service can call
``build_alerts`` after it has read the authoritative ledger and hand each
payload to the existing Feishu delivery queue.  It does not create an inbox
report, review card, or upload task by itself; an employee's subsequent
copy-and-fill message enters the normal report/review path.
"""
from __future__ import annotations

import datetime as dt
import base64
import fcntl
import hashlib
import json
import subprocess
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from zoneinfo import ZoneInfo

import coverage_tables
import alert_schedule

GROUP = 'oc_1f8587b1bcde12a0d1bb6053ab2b748a'
BACKFILL_CARD_VERSION = 'V1.15-CARD-6'
DEFAULT_DB = str(Path.home() / '.openclaw/state/production-parallel/inbox.sqlite')
STATUS_SNAPSHOT_NAME = 'pending_status.json'


def date_map_from_ledger(markdown: str) -> dict[str, set[str]]:
    """Extract effective employee dates from a markdown ledger snapshot.

    Only detail record IDs (``YYYYMMDD-A-001`` etc.) are considered.  Summary
    tables and coverage prose cannot create an alert, which keeps the reminder
    source tied to the same effective rows used by the writer.
    """
    import re

    result = {code: set() for code in coverage_tables.WORKERS}
    if not isinstance(markdown, str):
        return result
    for code in result:
        start = re.search(rf'^## {re.escape(code)}｜[^\n]+$', markdown, re.M)
        if not start:
            continue
        tail = markdown[start.end():]
        stop = re.search(r'^## ', tail, re.M)
        section = tail[:stop.start()] if stop else tail
        for match in re.finditer(rf'^\| (20\d{{6}}-{code}-\d{{3}}) \|', section, re.M):
            identifier = match.group(1)
            result[code].add(
                f"{identifier[:4]}-{identifier[4:6]}-{identifier[6:8]}"
            )
    return result


def build_alerts(
    date_map: Mapping[str, Iterable[str]],
    *,
    expected: Mapping[str, Iterable[str | dt.date]] | None = None,
    window_start: str | dt.date | None = None,
    window_end: str | dt.date | None = None,
    workers: Iterable[str] | None = None,
    now: dt.datetime | None = None,
) -> list[dict[str, str]]:
    """Return one alert payload per explicitly expected missing date.

    The default covers A/B and long-term C.  A caller that has an
    authoritative attendance window for D can pass ``workers=("D",)``
    together with an explicit ``expected`` map.  This keeps the irregular
    worker available for the same backfill flow without guessing that every
    blank day was a missed report.  A stable ``alert_key``
    (worker + date) lets the caller persist and suppress a reminder after the
    employee replies or the date is filled.
    """
    expected_map = expected or coverage_tables.expected_dates(
        date_map, window_start=window_start, window_end=window_end
    )
    missing = coverage_tables.missing_dates(date_map, expected_map)
    alert_workers = set(workers or coverage_tables.AUTO_ALERT_WORKERS)
    unknown = alert_workers - set(coverage_tables.WORKERS)
    if unknown:
        raise ValueError(f"未知员工代号：{'、'.join(sorted(unknown))}")
    alerts: list[dict[str, str]] = []
    current = now or dt.datetime.now(ZoneInfo("Asia/Shanghai"))
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    for code in sorted(alert_workers):
        for value in sorted(missing.get(code, ())):
            date = value.isoformat()
            check_at = alert_schedule.scheduled_check_at(
                code, value, tzinfo=current.tzinfo
            )
            if check_at is not None and current < check_at:
                continue
            alerts.append({
                "alert_key": f"{code}:{date}",
                "worker": code,
                "name": coverage_tables.WORKERS[code],
                "production_date": date,
                "status": "待核实",
                "check_at": check_at.isoformat() if check_at else "",
                "message": coverage_tables.backfill_reminder(code, [date]),
            })
    return alerts


def active_alert_keys(
    date_map: Mapping[str, Iterable[str]],
    **kwargs,
) -> set[str]:
    """Return stable keys for persistence/reconciliation layers."""
    return {item["alert_key"] for item in build_alerts(date_map, **kwargs)}


def _ensure_delivery_table(db: str) -> None:
    import queue_store as q
    q.init(db)
    with q.conn(db) as c:
        c.execute('''CREATE TABLE IF NOT EXISTS scheduled_missing_alerts(
          alert_key TEXT PRIMARY KEY,
          worker TEXT NOT NULL,
          production_date TEXT NOT NULL,
          message TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending',
          message_id TEXT,
          sent_at REAL,
          receipt_confirmed_at REAL,
          receipt_message_id TEXT,
          last_error TEXT,
          created REAL NOT NULL)''')
        for column, spec in (('receipt_confirmed_at', 'REAL'), ('receipt_message_id', 'TEXT')):
            try:
                c.execute(f'ALTER TABLE scheduled_missing_alerts ADD COLUMN {column} {spec}')
            except Exception:
                pass
        # Persist the platform idempotency key used for the current delivery.
        # A recalled card must be sent with a new key, while a transient
        # retry keeps the same key and cannot create a duplicate card.
        try:
            c.execute('ALTER TABLE scheduled_missing_alerts ADD COLUMN delivery_uuid TEXT')
        except Exception:
            pass


def refresh_status_snapshot(db: str = DEFAULT_DB, *, now: dt.datetime | None = None,
                            ledger_markdown: str | None = None,
                            alerts: Iterable[Mapping[str, str]] = ()) -> dict:
    """Persist a small, model-free queue snapshot for restart-safe status.

    The service calls this on its normal recovery loop.  The file is replaced
    only when the serialized state changes, so frequent polling does not
    create noisy writes or consume model/API tokens.  It is derived from the
    durable SQLite alert rows and is informational; production uploads still
    require the normal employee confirmation and GitHub readback guard.
    """
    _ensure_delivery_table(db)
    current = now or dt.datetime.now(ZoneInfo('Asia/Shanghai'))
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo('Asia/Shanghai'))
    import queue_store as q
    with q.conn(db) as c:
        rows = [dict(row) for row in c.execute(
            "SELECT alert_key,worker,production_date,state,message_id,receipt_confirmed_at,last_error "
            "FROM scheduled_missing_alerts ORDER BY worker,production_date").fetchall()]
    pending = [row for row in rows if row['state'] in {'pending', 'failed', 'sent'}]
    by_worker = {}
    for row in rows:
        by_worker.setdefault(row['worker'], []).append({
            'production_date': row['production_date'],
            'state': row['state'],
            'message_id': row['message_id'],
            'last_error': row['last_error'],
        })
    snapshot = {
        'schema': 1,
        'refreshed_at': current.isoformat(),
        'closed_through': (current.date() - dt.timedelta(days=1)).isoformat(),
        'pending': pending,
        'by_worker': by_worker,
        'new_alerts': [dict(item) for item in alerts],
    }
    path = Path(db).with_name(STATUS_SNAPSHOT_NAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
    try:
        previous = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        previous = None
    # ``refreshed_at`` is intentionally excluded from the comparison: a
    # timer tick alone must not rewrite the file.
    if previous:
        try:
            old = json.loads(previous)
            old.pop('refreshed_at', None)
            current_state = dict(snapshot)
            current_state.pop('refreshed_at', None)
            if old == current_state:
                return snapshot
        except (TypeError, ValueError):
            pass
    temporary = path.with_suffix('.tmp')
    temporary.write_text(serialized, encoding='utf-8')
    temporary.replace(path)
    return snapshot


def sync_pending_queue_to_github(
    db: str = DEFAULT_DB, *, now: dt.datetime | None = None,
) -> list[dict[str, str]]:
    """Mirror the durable pending queue into the ledger coverage section.

    This is a status-only synchronization.  It never creates a production
    detail row, changes quantities, or treats a pending date as an effective
    production day.  A meaningful queue change produces one guarded GitHub
    commit per affected year; a timer tick with identical state produces no
    commit.
    """
    path = Path(db).with_name(STATUS_SNAPSHOT_NAME)
    try:
        snapshot = json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError, TypeError, ValueError):
        return []
    pending = [
        item for item in snapshot.get('pending', [])
        if item.get('state') in {'pending', 'failed', 'sent'}
        and item.get('worker') and item.get('production_date')
    ]
    current = now or dt.datetime.now(ZoneInfo('Asia/Shanghai'))
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo('Asia/Shanghai'))

    import commit_guard
    # Import lazily to avoid the service -> missing_alerts import cycle.
    from deterministic_upload import _update_coverage

    results: list[dict[str, str]] = []
    for year, endpoint in commit_guard.LEDGER_ENDPOINTS.items():
        year_pending = [
            item for item in pending
            if str(item.get('production_date', '')).startswith(f'{year}-')
        ]
        try:
            remote = commit_guard.gh_read_json(endpoint)
            expected_sha = remote['sha']
            before = base64.b64decode(remote['content']).decode('utf-8')
            candidate = _update_coverage(
                before, '', set(), current.date().isoformat(),
                pending_queue=year_pending,
            )
            if candidate == before:
                results.append({'year': str(year), 'status': 'unchanged'})
                continue

            # Serialize with normal production uploads.  The guard validates
            # that this coverage-only rewrite preserves every detail row and
            # every aggregate quantity before the remote PUT.
            lock = Path(db).parent / 'ledger-write.lock'
            with lock.open('a', encoding='utf-8') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                latest = commit_guard.gh_read_json(endpoint)
                if latest['sha'] != expected_sha:
                    raise RuntimeError('GitHub 台账已更新，状态候选需重新读取')
                guard_note = commit_guard.run_original_ledger_guard(before, candidate)
                payload = {
                    'message': f'同步生产报数待核实状态（{year}）',
                    'sha': expected_sha,
                    'branch': 'main',
                    'content': base64.b64encode(candidate.encode()).decode(),
                }
                request = subprocess.run(
                    [commit_guard.GH, 'api', '--method', 'PUT', endpoint, '--input', '-'],
                    input=json.dumps(payload, ensure_ascii=False),
                    text=True, capture_output=True,
                )
                if request.returncode:
                    detail = (request.stderr or request.stdout or 'GitHub 状态同步失败').strip()
                    raise RuntimeError(detail[:300])
                response = json.loads(request.stdout)
                commit = response.get('commit', {}).get('sha')
                if not commit:
                    raise RuntimeError('GitHub 状态同步未返回 commit')
                reread = commit_guard.gh_read_json(endpoint)
                actual = base64.b64decode(reread['content']).decode('utf-8')
                if actual != candidate:
                    raise RuntimeError('GitHub 状态同步后回读不一致')
            receipt_dir = Path(db).parent / 'receipts'
            receipt_dir.mkdir(exist_ok=True)
            (receipt_dir / f'status-sync-{year}.json').write_text(
                json.dumps({
                    'status': 'verified', 'year': year, 'commit': commit,
                    'endpoint': endpoint, 'guard': guard_note,
                    'pending_count': len(year_pending),
                    'updated_at': current.isoformat(),
                }, ensure_ascii=False, indent=2),
                encoding='utf-8',
            )
            results.append({'year': str(year), 'status': 'verified', 'commit': commit})
        except Exception as exc:
            results.append({'year': str(year), 'status': 'failed', 'error': str(exc)[:300]})
    return results


def has_pending_alert(worker: str, production_date: str, db: str = DEFAULT_DB) -> bool:
    _ensure_delivery_table(db)
    import queue_store as q
    with q.conn(db) as c:
        row = c.execute("SELECT 1 FROM scheduled_missing_alerts WHERE worker=? AND production_date=? AND state IN ('pending','failed','sent') LIMIT 1",
                        (worker, production_date)).fetchone()
    return bool(row)


def mark_receipt_confirmed(worker: str, production_date: str, receipt_message_id: str | None = None,
                           db: str = DEFAULT_DB) -> bool:
    """Unlock the next date only after the Feishu success receipt was sent."""
    # The delivery message ID is the durable proof that the success receipt
    # was actually accepted by Feishu.  A GitHub commit alone must never
    # unlock the next date.
    if not receipt_message_id:
        return False
    _ensure_delivery_table(db)
    import queue_store as q
    with q.conn(db) as c:
        row = c.execute("SELECT alert_key FROM scheduled_missing_alerts WHERE worker=? AND production_date=? AND state='sent' LIMIT 1",
                        (worker, production_date)).fetchone()
        if not row:
            return False
        c.execute("""UPDATE scheduled_missing_alerts
                     SET state='completed',receipt_confirmed_at=?,receipt_message_id=?,last_error=NULL
                     WHERE alert_key=? AND state='sent'""",
                  (dt.datetime.now(ZoneInfo('Asia/Shanghai')).timestamp(), receipt_message_id, row['alert_key']))
    return True


def _ledger_snapshot(endpoint: str | None = None) -> str:
    """Read the authoritative remote ledger without writing it."""
    import commit_guard

    value = commit_guard.gh_read_json(endpoint or commit_guard.ENDPOINT)
    return base64.b64decode(value['content']).decode('utf-8')


def deliver_scheduled_alerts(
    db: str = DEFAULT_DB,
    *,
    now: dt.datetime | None = None,
    ledger_markdown: str | None = None,
) -> list[str]:
    """Create and deliver due one-worker/one-date reminders exactly once.

    The scheduler is deliberately separate from manual backfill.  It only
    creates a reminder card; the employee's reply still enters the existing
    review, confirmation, GitHub commit, remote-readback, and Feishu receipt
    flow.  A stable Feishu UUID makes a retry safe if the process is
    interrupted after the platform accepts a message.
    """
    _ensure_delivery_table(db)
    current = now or dt.datetime.now(ZoneInfo('Asia/Shanghai'))
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo('Asia/Shanghai'))
    if ledger_markdown is None:
        try:
            import commit_guard
            endpoint = commit_guard.endpoint_for_date(current.date().isoformat())
            ledger_markdown = _ledger_snapshot(endpoint)
        except Exception:
            return []
    date_map = date_map_from_ledger(ledger_markdown)
    # A production date becomes eligible after its next calendar day reaches
    # noon.  This keeps yesterday eligible while never treating today's open
    # date as missing before tomorrow noon.
    closed_through = current.date() - dt.timedelta(days=1)
    alerts = build_alerts(
        date_map,
        window_end=closed_through,
        now=current,
    )
    import queue_store as q
    _ensure_delivery_table(db)
    with q.conn(db) as c:
        for alert in alerts:
            c.execute(
                '''INSERT OR IGNORE INTO scheduled_missing_alerts
                   (alert_key,worker,production_date,message,created)
                   VALUES (?,?,?,?,?)''',
                 (alert['alert_key'], alert['worker'], alert['production_date'],
                 alert['message'], current.timestamp()),
            )
    refresh_status_snapshot(db, now=current, ledger_markdown=ledger_markdown, alerts=alerts)
    if not alerts:
        return []
    try:
        from transport import request
        import card_builder
    except ImportError:
        return []
    delivered: list[str] = []
    with q.conn(db) as c:
        # Keep one worker/date card in flight.  A later missing date remains
        # queued until the earlier date disappears from the authoritative
        # ledger on the next scan (which proves the GitHub upload/readback
        # completed).  This prevents a multi-day gap from being bulk-sent.
        all_rows = c.execute(
            "SELECT * FROM scheduled_missing_alerts "
            "WHERE state IN ('pending','failed','sent') "
            "ORDER BY worker, production_date"
        ).fetchall()
        active_keys = {alert['alert_key'] for alert in alerts}
        for row in all_rows:
            # Feishu keeps a recalled message ID resolvable, so the local
            # state alone cannot tell whether the employee still has a card.
            # Treat a confirmed platform deletion as a fresh pending send.
            # Lookup failures are deliberately ignored: an uncertain GET
            # must not create a duplicate card.
            if row['state'] == 'sent' and row['message_id']:
                try:
                    item = request('xiaowen', 'GET', f"/im/v1/messages/{row['message_id']}")
                    deleted = bool((item.get('data') or {}).get('items', [{}])[0].get('deleted'))
                except Exception:
                    deleted = False
                if deleted:
                    fresh_uuid = str(uuid.uuid4())
                    c.execute(
                        "UPDATE scheduled_missing_alerts SET state='pending',message_id=NULL,sent_at=NULL,delivery_uuid=?,last_error=? WHERE alert_key=? AND state='sent'",
                        (fresh_uuid, 'previous Feishu card was recalled; scheduled resend', row['alert_key']),
                    )
            # A sent card is completed only through mark_receipt_confirmed,
            # after GitHub readback and the Feishu success receipt.
            if row['state'] == 'sent' and row['alert_key'] not in active_keys and row['receipt_confirmed_at']:
                c.execute(
                    "UPDATE scheduled_missing_alerts SET state='completed',last_error=NULL "
                    "WHERE alert_key=?", (row['alert_key'],)
                )
        remaining = c.execute(
            "SELECT * FROM scheduled_missing_alerts "
            "WHERE state IN ('pending','failed','sent') ORDER BY worker, production_date"
        ).fetchall()
        first_by_worker = {}
        for row in remaining:
            # The first row may still be sent/in-flight.  In that case later
            # dates for the same worker stay blocked until the next scan
            # marks the first row completed from the ledger.
            first_by_worker.setdefault(row['worker'], row)
        rows = [row for row in first_by_worker.values()
                if row['state'] in ('pending', 'failed')]
        for row in rows:
            key = row['alert_key']
            uuid_key = row['delivery_uuid'] or str(uuid5(key))
            if not row['delivery_uuid']:
                c.execute("UPDATE scheduled_missing_alerts SET delivery_uuid=? WHERE alert_key=? AND state IN ('pending','failed')",
                          (uuid_key, key))
            try:
                pending_dates = [item['production_date'] for item in remaining
                                 if item['worker'] == row['worker']]
                # The scheduler starts the same verification state machine as
                # the manual entry point.  The six-field input template is a
                # second-stage card after choice 3; sending it here skipped
                # choices 1/2 and made automatic reminders diverge from the
                # required flow.
                import backfill_flow
                payload = backfill_flow.verification_card(
                    row['worker'], row['production_date'], pending_dates=pending_dates
                )
                result = request('xiaowen', 'POST',
                    '/im/v1/messages?receive_id_type=chat_id', {
                        'receive_id': GROUP,
                        'msg_type': 'interactive',
                        'content': json.dumps(payload, ensure_ascii=False),
                        'uuid': uuid_key,
                    })
                message_id = result.get('data', {}).get('message_id')
                c.execute(
                    "UPDATE scheduled_missing_alerts SET state='sent',message_id=?,"
                    "sent_at=?,last_error=NULL WHERE alert_key=?",
                    (message_id, current.timestamp(), key),
                )
                try:
                    import backfill_flow
                    backfill_flow.register_verification(row['worker'], row['production_date'], message_id)
                except Exception as exc:
                    c.execute("UPDATE scheduled_missing_alerts SET last_error=? WHERE alert_key=?",
                              ('manual-state-register: ' + str(exc)[:160], key))
                delivered.append(key)
            except Exception as exc:
                c.execute(
                    "UPDATE scheduled_missing_alerts SET state='failed',last_error=? "
                    "WHERE alert_key=?", (type(exc).__name__ + ': ' + str(exc)[:180], key)
                )
    return delivered


def uuid5(value: str) -> str:
    """Return the deterministic UUID used for a scheduled alert delivery."""
    return str(__import__('uuid').uuid5(__import__('uuid').NAMESPACE_URL,
                                        f'production-missing-alert:{BACKFILL_CARD_VERSION}:{GROUP}:{value}'))


if __name__ == "__main__":
    import argparse
    import json
    import sys
    parser = argparse.ArgumentParser()
    parser.add_argument('command', nargs='?', default='build')
    parser.add_argument('--worker')
    parser.add_argument('--date')
    parser.add_argument('--receipt-message-id')
    args = parser.parse_args()
    if args.command == 'receipt-confirmed':
        print(json.dumps({'updated': mark_receipt_confirmed(args.worker, args.date, args.receipt_message_id)}, ensure_ascii=False))
    else:
        payload = json.load(sys.stdin)
        print(json.dumps(build_alerts(payload), ensure_ascii=False))
