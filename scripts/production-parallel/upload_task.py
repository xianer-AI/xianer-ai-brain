"""Machine-bound upload tasks: inspect first, then reuse the existing commit guard.

Long platform IDs are read from an immutable task file after the gateway has
validated their durable confirmation binding. No command guesses, repairs, or
substitutes an ID, and prepare/inspect never write the remote ledger.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile

import commit_guard
from service import DB

SCHEMA = 'production-upload-task/v1'
FIELDS = frozenset(('schema', 'source', 'confirmation', 'group'))
RECEIPT_STATES = frozenset(('pending', 'pending_dispatch', 'dispatching', 'dispatched'))


def task_directory(db):
    return Path(db).resolve().parent / 'upload-tasks'


def task_filename(source, confirmation):
    pair = json.dumps([source, confirmation], separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(pair).hexdigest()[:24] + '.json'


def validate_fields(task):
    if not isinstance(task, dict) or set(task) != FIELDS or task.get('schema') != SCHEMA:
        raise ValueError('上传任务格式或版本不匹配')
    if not isinstance(task['source'], str) or not re.fullmatch(r'om_[A-Za-z0-9_-]+', task['source']):
        raise ValueError('上传任务原始报数 ID 无效')
    if not isinstance(task['confirmation'], str) or not re.fullmatch(r'(?:om_|card-action-)[A-Za-z0-9_-]+', task['confirmation']):
        raise ValueError('上传任务确认消息 ID 无效')
    if not isinstance(task['group'], str) or not re.fullmatch(r'oc_[A-Za-z0-9_-]+', task['group']):
        raise ValueError('上传任务群 ID 无效')
    return task


def validate_binding(db, task):
    """Validate exact durable IDs; preserve every existing guard check."""
    validate_fields(task)
    # Open read-only so a typo in the database path cannot create an empty DB.
    with sqlite3.connect(Path(db).resolve().as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        receipt = conn.execute(
            'SELECT * FROM confirmation_receipts WHERE message_id=?',
            (task['confirmation'],),
        ).fetchone()
    if not receipt:
        raise ValueError('指定确认消息没有持久化确认回执')
    receipt = dict(receipt)
    if receipt['source'] != task['source'] or receipt['grp'] != task['group']:
        raise ValueError('上传任务与确认回执的来源或群绑定不一致')
    if receipt['status'] not in RECEIPT_STATES or not receipt.get('token'):
        raise ValueError('确认回执未处于有效上传状态')
    source, confirmation = commit_guard.validate(db, task['source'], task['confirmation'])
    if source['grp'] != task['group'] or confirmation['grp'] != task['group']:
        raise ValueError('上传任务与收件记录的群不一致')
    if receipt['sender'] != confirmation['sender']:
        raise ValueError('确认回执与确认消息的发送人不一致')
    with sqlite3.connect(Path(db).resolve().as_uri() + '?mode=ro', uri=True) as conn:
        card = conn.execute(
            "SELECT 1 FROM review_cards WHERE token=? AND source=? AND callback=? AND state='confirmed'",
            (receipt['token'], task['source'], task['confirmation']),
        ).fetchone()
    if not card:
        raise ValueError('确认回执没有对应的有效核对卡绑定')
    return source, confirmation, receipt


def _result(path, task, source, confirmation, receipt):
    endpoint = commit_guard.endpoint_for_date(commit_guard.report_from_inbox_row(source).get('production_date'))
    return {
        'task': str(path), 'path': str(path), **task,
        'report': (source.get('result') or {}).get('extracted'),
        'source_record': source,
        'confirmation_record': confirmation,
        'receipt_status': receipt['status'],
        'ledger_endpoint': endpoint,
        'ledger_filename': Path(endpoint).name,
        'validated': True,
    }


def prepare(db, source, confirmation, group):
    task = {'schema': SCHEMA, 'source': source, 'confirmation': confirmation, 'group': group}
    original, confirmed, receipt = validate_binding(db, task)
    directory = task_directory(db)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / task_filename(source, confirmation)
    serialized = json.dumps(task, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
    # Link a completely written file into place, atomically and without
    # replacing a task created by another worker for the same exact pair.
    fd, temporary = tempfile.mkstemp(prefix='.upload-task-', dir=str(directory))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, str(path))
        except FileExistsError:
            if path.is_symlink() or path.read_text(encoding='utf-8') != serialized:
                raise ValueError('已有上传任务内容冲突，禁止覆盖')
    finally:
        Path(temporary).unlink(missing_ok=True)
    return _result(path, task, original, confirmed, receipt)


def inspect(db, task_path):
    path = Path(task_path)
    if path.is_symlink():
        raise ValueError('上传任务不能使用符号链接')
    path = path.resolve()
    if path.parent != task_directory(db):
        raise ValueError('上传任务必须位于当前生产收件箱的 upload-tasks 目录')
    if path.stat().st_size > 4096:
        raise ValueError('上传任务文件大小异常')
    task = validate_fields(json.loads(path.read_text(encoding='utf-8')))
    if path.name != task_filename(task['source'], task['confirmation']):
        raise ValueError('上传任务文件名与绑定消息不一致')
    source, confirmation, receipt = validate_binding(db, task)
    return _result(path, task, source, confirmation, receipt)


def commit(db, task_path, expected_sha, candidate, review_note):
    """Use task-bound IDs only; the existing guard owns every remote write."""
    task = inspect(db, task_path)
    args = [
        sys.executable, str(Path(commit_guard.__file__).resolve()),
        '--mode', 'new', '--source', task['source'],
        '--confirmation', task['confirmation'], '--expected-sha', expected_sha,
        '--file', str(candidate), '--review-note', review_note,
    ]
    environment = dict(os.environ)
    environment['PRODUCTION_INBOX_DB'] = str(Path(db).resolve())
    environment['PRODUCTION_LEDGER_ENDPOINT'] = task['ledger_endpoint']
    return subprocess.run(args, env=environment, check=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('prepare')
    create.add_argument('--source', required=True)
    create.add_argument('--confirmation', required=True)
    create.add_argument('--group', required=True)
    check = commands.add_parser('inspect')
    check.add_argument('--task', required=True)
    write = commands.add_parser('commit')
    write.add_argument('--task', required=True)
    write.add_argument('--expected-sha', required=True)
    write.add_argument('--file', required=True)
    write.add_argument('--review-note', required=True)
    args = parser.parse_args(argv)
    if args.command == 'prepare':
        result = prepare(DB, args.source, args.confirmation, args.group)
    elif args.command == 'inspect':
        result = inspect(DB, args.task)
    else:
        commit(DB, args.task, args.expected_sha, args.file, args.review_note)
        return
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, sqlite3.Error) as exc:
        print('上传任务校验失败：' + str(exc), file=sys.stderr)
        sys.exit(1)
