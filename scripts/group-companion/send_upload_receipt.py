"""Send one idempotent verified production receipt to the Feishu group."""
import argparse
import json
import sys
import uuid
from pathlib import Path

PRODUCTION_DIR = Path(__file__).resolve().parent.parent / 'production-parallel'
if str(PRODUCTION_DIR) not in sys.path:
    sys.path.insert(0, str(PRODUCTION_DIR))

from card_builder import receipt_payload
from transport import request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--receipt')
    parser.add_argument('--receipt-json')
    parser.add_argument('--group', required=True)
    args = parser.parse_args()
    if bool(args.receipt) == bool(args.receipt_json):
        raise ValueError('必须提供一个上传回执来源')
    receipt = json.loads(args.receipt_json) if args.receipt_json else json.loads(Path(args.receipt).read_text(encoding='utf-8'))
    text = str(receipt.get('message') or '').strip()
    source = str(receipt.get('source') or '')
    confirmation = str(receipt.get('confirmation') or '')
    if receipt.get('status') != 'verified' or not text or not source or not confirmation:
        raise ValueError('没有可发送的已验证上传回执')
    key = str(uuid.uuid5(uuid.NAMESPACE_URL, 'production-upload-receipt:' + source + ':' + confirmation))
    result = request('xiaowen', 'POST', '/im/v1/messages?receive_id_type=chat_id', {
        'receive_id': args.group,
        'msg_type': 'interactive',
        'content': json.dumps(receipt_payload(text, backfill=bool(receipt.get('backfill'))), ensure_ascii=False),
        'uuid': key,
    })
    print(json.dumps({'message_id': result['data']['message_id']}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('发送上传回执失败：' + str(exc), file=sys.stderr)
        raise SystemExit(1)
