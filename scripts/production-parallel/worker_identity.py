"""Employee identity bindings for the Feishu production group.

Employees can self-register by sending an exact declaration such as ``B=梅芳``.
The first declaration creates the binding; conflicting account/code bindings are
rejected and never overwritten. The legacy owner-confirmed command remains
available for recovery and backwards compatibility.
"""
import json
import re
import sqlite3
import tempfile
import time
from pathlib import Path

OWNER='ou_de130236fb86ee826e0f5653f05bc9c6'
GROUP='oc_1f8587b1bcde12a0d1bb6053ab2b748a'
NAMES={'A':'徐超超','B':'梅芳','C':'李鸿玉','D':'张小翠'}
SELF_BIND_RE=re.compile(r'^\s*([ABCD])\s*[=:：]\s*(徐超超|梅芳|李鸿玉|张小翠)\s*$')
LEGACY_STATE=Path.home()/'.openclaw/state/production-sync/state.json'


def init(db):
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE IF NOT EXISTS worker_identity(worker TEXT,name TEXT,platform TEXT PRIMARY KEY,owner TEXT,proof TEXT,created REAL)')


def _atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=path.name+'.', dir=path.parent)
    try:
        with open(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write('\n')
        Path(tmp).chmod(0o600)
        Path(tmp).replace(path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def _sync_legacy(platform, worker):
    """Keep the retired production-sync query path compatible with this binding."""
    try:
        state=json.loads(LEGACY_STATE.read_text())
    except Exception:
        state={'processed': {}, 'pending': {}}
    bindings=state.setdefault('workerBindings', {})
    old=bindings.get(platform)
    if old and old != worker:
        raise ValueError('此账号已绑定其他员工代号')
    other=next((account for account, code in bindings.items() if account != platform and code == worker), None)
    if other:
        raise ValueError(f'{worker} 已绑定其他账号')
    bindings[platform]=worker
    _atomic_json(LEGACY_STATE, state)


def bind(db, worker, platform, owner, proof):
    """Legacy explicit binding requiring the owner account."""
    init(db)
    if owner != OWNER:
        raise ValueError('只有老板确认才能绑定员工身份')
    name=NAMES.get(worker)
    if not name:
        raise ValueError('未知工人代号')
    with sqlite3.connect(db) as c:
        old=c.execute('SELECT worker,name FROM worker_identity WHERE platform=?',(platform,)).fetchone()
        if old and old[0] != worker:
            raise ValueError('此账号已绑定其他代号')
        old_worker=c.execute('SELECT platform FROM worker_identity WHERE worker=?',(worker,)).fetchone()
        if old_worker and old_worker[0] != platform:
            raise ValueError('此代号已绑定其他账号')
    _sync_legacy(platform, worker)
    with sqlite3.connect(db) as c:
        c.execute('INSERT OR REPLACE INTO worker_identity VALUES(?,?,?,?,?,?)',(worker,name,platform,owner,proof,time.time()))


def bind_verified(db, worker, platform, proof, owner='verified-by-account-audit'):
    """Register a previously verified sender account for future reports.

    This is for an explicit owner-side correction/audit only. Once written,
    the sender's stable Feishu open_id is the identity signal for later reports.
    """
    init(db)
    name=NAMES.get(worker)
    if not name:
        raise ValueError('未知工人代号')
    if not platform:
        raise ValueError('未识别发送人的飞书账号')
    with sqlite3.connect(db) as c:
        old=c.execute('SELECT worker,name FROM worker_identity WHERE platform=?',(platform,)).fetchone()
        if old and old[0] != worker:
            raise ValueError('此账号已绑定其他代号')
        old_worker=c.execute('SELECT platform FROM worker_identity WHERE worker=?',(worker,)).fetchone()
        if old_worker and old_worker[0] != platform:
            raise ValueError('此代号已绑定其他账号')
    _sync_legacy(platform, worker)
    with sqlite3.connect(db) as c:
        c.execute('INSERT OR REPLACE INTO worker_identity VALUES(?,?,?,?,?,?)',(worker,name,platform,owner,proof,time.time()))
    return {'bound':worker,'name':name,'platform':platform,'proof':proof,'mode':'verified-account-audit'}


def parse_self_declaration(text):
    m=SELF_BIND_RE.fullmatch(str(text or '').strip())
    if not m or NAMES.get(m.group(1)) != m.group(2):
        return None
    return m.group(1), m.group(2)


def self_bind(db, worker, platform, proof):
    """Bind the sender's own Feishu account without an owner confirmation."""
    init(db)
    name=NAMES.get(worker)
    if not name:
        raise ValueError('未知工人代号')
    if not platform:
        raise ValueError('未识别发送人的飞书账号')
    # Check the durable identity table before touching the legacy mirror, so
    # a conflicting binding is never partially written.
    with sqlite3.connect(db) as c:
        old=c.execute('SELECT worker,name FROM worker_identity WHERE platform=?',(platform,)).fetchone()
        if old and old[0] != worker:
            raise ValueError('此账号已绑定其他代号')
        old_worker=c.execute('SELECT platform FROM worker_identity WHERE worker=?',(worker,)).fetchone()
        if old_worker and old_worker[0] != platform:
            raise ValueError('此代号已绑定其他账号')
    _sync_legacy(platform, worker)
    with sqlite3.connect(db) as c:
        c.execute('INSERT OR REPLACE INTO worker_identity VALUES(?,?,?,?,?,?)',(worker,name,platform,'self-asserted',proof,time.time()))
    return {'bound':worker,'name':name,'platform':platform,'proof':proof,'mode':'self'}


def lookup(db, platform):
    init(db)
    with sqlite3.connect(db) as c:
        r=c.execute('SELECT worker,name,platform,proof FROM worker_identity WHERE platform=?',(platform,)).fetchone()
        return dict(zip(['worker','name','platform','proof'],r)) if r else None


if __name__=='__main__':
    import sys
    import queue_store as q
    from service import DB
    event=json.load(sys.stdin)
    text=str(event.get('text') or event.get('content') or '').strip()
    if event.get('mode')=='self_bind' or parse_self_declaration(text):
        parsed=parse_self_declaration(text)
        if not parsed:
            raise ValueError('格式应为 A=徐超超、B=梅芳、C=李鸿玉或 D=张小翠')
        worker,name=parsed
        group=event.get('group') or event.get('groupId')
        if group and group != GROUP:
            raise ValueError('只允许在生产统计群绑定')
        platform=event.get('sender') or event.get('senderId') or event.get('platform')
        print(json.dumps(self_bind(DB,worker,platform,event.get('id') or event.get('messageId') or ''),ensure_ascii=False))
        raise SystemExit(0)
    import re
    m=re.fullmatch(r'绑定员工\s+([ABCD])\s+(om_[A-Za-z0-9_-]+)',text)
    if not m:
        raise ValueError('格式：绑定员工 A 原报数消息ID')
    proof=q.get(DB,event.get('id') or event.get('messageId'));source=q.get(DB,m[2])
    if not proof or proof['sender']!=OWNER or proof['event']['content'].strip()!=text:
        raise ValueError('缺少老板本人原始确认')
    if not source or source['grp']!=proof['grp']:
        raise ValueError('找不到同群员工原报数')
    bind(DB,m[1],source['sender'],proof['sender'],event.get('id') or event.get('messageId'))
    print(json.dumps({'bound':m[1],'name':NAMES[m[1]],'mode':'owner'},ensure_ascii=False))
