"""Low-cost, idempotent health/recovery checks for the production pipeline.

This module never creates a production upload. It only identifies dead workers
so the existing durable confirmation queue can retry the exact message.
"""
import argparse
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import queue_store as q
import review_cards


def classify_receipt(row, verified=False, process_exists=None):
    if verified:
        return 'reconcile_verified'
    if row.get('status') != 'dispatching':
        return 'ignore'
    pid = row.get('worker_pid')
    if pid and process_exists and process_exists(pid):
        return 'wait'
    return 'requeue_dispatch'


def _pid_exists(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def inspect(db, now=None, process_exists=_pid_exists):
    """Return recovery actions and requeue only dead, unverified workers."""
    now = time.time() if now is None else now
    review_cards.init(db)
    actions = []
    for item in review_cards.retry_verified_replies(db, now):
        action = dict(item)
        action['watchdog_action'] = 'retry_verified_reply'
        actions.append(action)
    with q.conn(db) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM confirmation_receipts WHERE status='dispatching' AND next_at<=?",
            (now,)).fetchall()]
    for row in rows:
        verified = False
        try:
            review_cards.verify_dispatch_receipt(db, row['source'], row['message_id'])
            verified = True
        except Exception:
            pass
        action = classify_receipt(row, verified, process_exists)
        if action == 'requeue_dispatch':
            review_cards.mark_confirmation_dispatch_failed(db, row['message_id'], 'watchdog: worker 不存在，已回队列')
        actions.append({'action': action, 'message_id': row['message_id'], 'source': row.get('source')})
    return actions


def _service_running():
    result = subprocess.run(['pgrep', '-f', 'production-parallel/service.py serve'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=str(Path.home()/'.openclaw/state/production-parallel/inbox.sqlite'))
    parser.add_argument('--check-service', action='store_true')
    args = parser.parse_args()
    actions = inspect(args.db)
    result = {'actions': actions, 'service_running': _service_running()}
    print(json.dumps(result, ensure_ascii=False))
    if args.check_service and not result['service_running']:
        subprocess.run(['/bin/launchctl', 'kickstart', '-k', 'gui/%s/com.xianer.production-parallel' % os.getuid()],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
