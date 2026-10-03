#!/bin/bash
set -euo pipefail

SOURCE='om_backfill_status_B_2026-10-02_6c55f1e9cb688cf6'
CONFIRMATION='om_x100b633fdbc690a0b3ece7f065d481f'
WORKER='B'
PRODUCTION_DATE='2026-10-02'
DB="$HOME/.openclaw/state/production-parallel/inbox.sqlite"
REQUEST_DB="$HOME/.openclaw/state/production-parallel/backfill_flow.sqlite"
DESKTOP_ROOT="$HOME/Desktop/贤二Ai大脑知识库"
DESKTOP_DIR="$DESKTOP_ROOT/scripts/production-parallel"
WORKSPACE_DIR="$HOME/.openclaw/workspace/scripts/production-parallel"
RUNTIME_DIR="$HOME/.openclaw/production-runtime/scripts/production-parallel"
GROUP_COMPANION="$HOME/.openclaw/workspace/scripts/group-companion"
SERVICE_LABEL="gui/$(id -u)/com.xianer.production-parallel"
REPO='xianer-AI/xianer-ai-brain'

if [[ -x "$HOME/.hermes/hermes-agent/venv/bin/python" ]]; then
  PY="$HOME/.hermes/hermes-agent/venv/bin/python"
else
  PY="$(command -v python3)"
fi

for cmd in git rsync tar; do
  command -v "$cmd" >/dev/null || { echo "缺少命令: $cmd" >&2; exit 1; }
done
[[ -x "$PY" ]] || { echo "找不到可用 Python" >&2; exit 1; }
[[ -d "$DESKTOP_ROOT/.git" ]] || { echo "桌面知识库不是 Git 仓库: $DESKTOP_ROOT" >&2; exit 1; }
[[ -f "$DB" ]] || { echo "找不到生产数据库: $DB" >&2; exit 1; }

echo "== 1/9 获取 GitHub main 最新生产工作台 =="
cd "$DESKTOP_ROOT"
git fetch origin main
REMOTE_COMMIT="$(git rev-parse origin/main)"
echo "origin/main: $REMOTE_COMMIT"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/source"
git archive --format=tar origin/main scripts/production-parallel | tar -x -C "$TMP/source"
git show "origin/main:袜子生产制造袜子厂/库存记录/2026下半年下机白胚半成品统计.md" > /tmp/ledger-current.md

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$HOME/.openclaw/backups/production-parallel-$STAMP"
mkdir -p "$BACKUP"
for pair in "desktop:$DESKTOP_DIR" "workspace:$WORKSPACE_DIR" "runtime:$RUNTIME_DIR"; do
  name="${pair%%:*}"
  dir="${pair#*:}"
  if [[ -d "$dir" ]]; then
    cp -a "$dir" "$BACKUP/$name"
  fi
done
echo "备份: $BACKUP"

echo "== 2/9 同步三处 production-parallel 代码 =="
mkdir -p "$DESKTOP_DIR" "$WORKSPACE_DIR" "$RUNTIME_DIR"
rsync -a "$TMP/source/scripts/production-parallel/" "$DESKTOP_DIR/"
rsync -a "$TMP/source/scripts/production-parallel/" "$WORKSPACE_DIR/"
rsync -a "$TMP/source/scripts/production-parallel/" "$RUNTIME_DIR/"

echo "== 3/9 校验关键文件三端一致 =="
FILES=(deterministic_upload.py coverage_tables.py review_cards.py commit_guard.py upload_task.py missing_alerts.py backfill_flow.py)
for f in "${FILES[@]}"; do
  a="$(shasum -a 256 "$DESKTOP_DIR/$f" | awk '{print $1}')"
  b="$(shasum -a 256 "$WORKSPACE_DIR/$f" | awk '{print $1}')"
  c="$(shasum -a 256 "$RUNTIME_DIR/$f" | awk '{print $1}')"
  [[ "$a" == "$b" && "$b" == "$c" ]] || { echo "三端不一致: $f" >&2; exit 1; }
  echo "OK $f $a"
done

echo "== 4/9 运行专项测试和完整回归测试 =="
cd "$DESKTOP_DIR"
PYTHONDONTWRITEBYTECODE=1 "$PY" -m unittest   test_deterministic_upload.ProductionStatusRegressionTests   test_coverage_tables.CoverageTableTests.test_confirmed_status_suppresses_stale_pending_queue
PYTHONDONTWRITEBYTECODE=1 "$PY" -m unittest discover -p 'test_*.py'

echo "== 5/9 初始化恢复规则并重载生产服务 =="
PYTHONPATH="$WORKSPACE_DIR" "$PY" - <<'PY'
from service import DB
import review_cards
review_cards.init(DB)
print("review_cards migration/init: OK")
PY
/bin/launchctl kickstart -k "$SERVICE_LABEL"
sleep 2
/bin/launchctl print "$SERVICE_LABEL" | grep -E 'state =|pid =|last exit' | head -20 || true

echo "== 6/9 用原确认任务执行远程对账恢复（禁止重复 PUT） =="
TASK_PATH="$(PYTHONPATH="$WORKSPACE_DIR" "$PY" - <<PY
import upload_task
print(upload_task.task_directory("$DB") / upload_task.task_filename("$SOURCE", "$CONFIRMATION"))
PY
)"
[[ -f "$TASK_PATH" ]] || { echo "找不到原上传任务: $TASK_PATH" >&2; exit 1; }

RECOVERY_JSON="$(PYTHONDONTWRITEBYTECODE=1 "$PY" "$WORKSPACE_DIR/deterministic_upload.py" --task "$TASK_PATH")"
echo "$RECOVERY_JSON"
RECEIPT_STATUS="$(printf '%s' "$RECOVERY_JSON" | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("status",""))')"
[[ "$RECEIPT_STATUS" == "verified" ]] || { echo "远程对账恢复未得到 verified" >&2; exit 1; }

echo "== 7/9 发送或复用飞书成功回执，并完成本地 dispatched 状态 =="
STATE_JSON="$(DB="$DB" CONFIRMATION="$CONFIRMATION" "$PY" - <<'PY'
import json, os, sqlite3
db=os.environ["DB"]; mid=os.environ["CONFIRMATION"]
with sqlite3.connect(db) as c:
    c.row_factory=sqlite3.Row
    row=c.execute("SELECT status,grp,reply_message_id FROM confirmation_receipts WHERE message_id=?",(mid,)).fetchone()
print(json.dumps(dict(row) if row else None, ensure_ascii=False))
PY
)"
echo "confirmation_receipt before: $STATE_JSON"

CURRENT_STATUS="$(printf '%s' "$STATE_JSON" | "$PY" -c 'import json,sys; x=json.load(sys.stdin); print((x or {}).get("status",""))')"
REPLY_ID="$(printf '%s' "$STATE_JSON" | "$PY" -c 'import json,sys; x=json.load(sys.stdin); print((x or {}).get("reply_message_id") or "")')"
GROUP_ID="$(printf '%s' "$STATE_JSON" | "$PY" -c 'import json,sys; x=json.load(sys.stdin); print((x or {}).get("grp") or "")')"

if [[ "$CURRENT_STATUS" == "dispatched" && -n "$REPLY_ID" ]]; then
  echo "已有飞书成功回执: $REPLY_ID"
else
  [[ -n "$GROUP_ID" ]] || { echo "确认回执缺少群 ID" >&2; exit 1; }
  VERIFY_JSON="$(PYTHONDONTWRITEBYTECODE=1 "$PY" "$WORKSPACE_DIR/review_cards.py" verify-dispatch --source "$SOURCE" --confirmation "$CONFIRMATION")"
  SEND_JSON="$(PYTHONDONTWRITEBYTECODE=1 "$PY" "$GROUP_COMPANION/send_upload_receipt.py" --group "$GROUP_ID" --receipt-json "$VERIFY_JSON")"
  echo "$SEND_JSON"
  REPLY_ID="$(printf '%s' "$SEND_JSON" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["message_id"])')"
  PYTHONDONTWRITEBYTECODE=1 "$PY" "$WORKSPACE_DIR/review_cards.py" mark-dispatched     --message-id "$CONFIRMATION" --reply-message-id "$REPLY_ID"
fi

echo "== 8/9 完成补报/待核实队列闭环 =="
PYTHONDONTWRITEBYTECODE=1 "$PY" "$WORKSPACE_DIR/backfill_flow.py" advance   --worker "$WORKER" --completed-date "$PRODUCTION_DATE" --receipt-message-id "$REPLY_ID"

# 刷新本地状态快照；如果队列已完成，它不会再把 B/10-02 写回待核实。
PYTHONPATH="$WORKSPACE_DIR" "$PY" - <<'PY'
import missing_alerts
snapshot=missing_alerts.refresh_status_snapshot()
print("pending snapshot rows:", len(snapshot.get("pending", [])))
PY

echo "== 9/9 最终验收 =="
PYTHONPATH="$WORKSPACE_DIR" SOURCE="$SOURCE" CONFIRMATION="$CONFIRMATION" PRODUCTION_DATE="$PRODUCTION_DATE" DB="$DB" REQUEST_DB="$REQUEST_DB" "$PY" - <<'PY'
import base64, json, os, sqlite3
import commit_guard

source=os.environ["SOURCE"]
confirmation=os.environ["CONFIRMATION"]
day=os.environ["PRODUCTION_DATE"]
db=os.environ["DB"]
request_db=os.environ["REQUEST_DB"]

endpoint=commit_guard.endpoint_for_date(day)
remote=commit_guard.gh_read_json(endpoint)
text=base64.b64decode(remote["content"]).decode("utf-8")

checks={
  "remote_status_row": "| B｜梅芳 | 2026年10月2日 | 已确认未上班（不计入生产统计） |" in text,
  "remote_source": source in text,
  "remote_confirmation": confirmation in text,
  "remote_no_stale_pending_row": "| B｜梅芳 | 2026年10月2日 | 核实卡已发送，等待回复 |" not in text,
}

with sqlite3.connect(db) as c:
    c.row_factory=sqlite3.Row
    cr=c.execute("SELECT status,reply_message_id,error,error_detail FROM confirmation_receipts WHERE message_id=?",(confirmation,)).fetchone()
    alert=c.execute("SELECT state,receipt_message_id,last_error FROM scheduled_missing_alerts WHERE worker='B' AND production_date=?",(day,)).fetchone()
checks["confirmation_receipt_dispatched"]=bool(cr and cr["status"]=="dispatched" and cr["reply_message_id"])
checks["scheduled_alert_completed"]=bool(alert and alert["state"]=="completed" and alert["receipt_message_id"])

request=None
if os.path.exists(request_db):
    with sqlite3.connect(request_db) as c:
        c.row_factory=sqlite3.Row
        request=c.execute("SELECT status FROM requests WHERE worker='B' AND production_date=? ORDER BY updated_at DESC LIMIT 1",(day,)).fetchone()
checks["backfill_request_uploaded"]=bool(request and request["status"]=="uploaded")

print(json.dumps({
  "checks":checks,
  "confirmation_receipt":dict(cr) if cr else None,
  "scheduled_alert":dict(alert) if alert else None,
  "backfill_request":dict(request) if request else None,
  "remote_sha":remote.get("sha"),
},ensure_ascii=False,indent=2))

failed=[name for name,value in checks.items() if not value]
if failed:
    raise SystemExit("最终验收失败: "+", ".join(failed))
print("FINAL_OK: 本机+飞书+GitHub 全链路验收通过")
PY
