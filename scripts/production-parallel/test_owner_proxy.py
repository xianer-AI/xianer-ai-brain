import json
import tempfile
import unittest
from pathlib import Path

import queue_store as q
import review_cards


class OwnerProxyCardTests(unittest.TestCase):
    def test_owner_explicit_proxy_can_create_card_with_proxy_label(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / 'inbox.sqlite')
            q.init(db)
            review_cards.init(db)
            source = 'om_proxy_source'
            q.put(db, {
                'messageId': source, 'senderId': review_cards.OWNER,
                'groupId': review_cards.GROUP,
                'content': '代报：B=梅芳\n冰冰袜1700双\n棉堆堆袜2500双\n小腿袜300双\n过膝袜0\n女船袜0\n男船袜0',
            })
            result = {'extracted': {'kind': 'report', 'worker': 'B', 'proxy': True,
                                    'production_date': '2026-09-27', 'items': [
                                        {'product': product, 'process': '下机', 'quantity': quantity}
                                        for product, quantity in (
                                            ('冰冰袜', 1700), ('棉堆堆袜', 2500),
                                            ('小腿袜', 300), ('过膝袜', 0),
                                            ('女船袜', 0), ('男船袜', 0))]}}
            with q.conn(db) as conn:
                conn.execute('UPDATE inbox SET status="ready", result=? WHERE id=?',
                             (json.dumps(result, ensure_ascii=False), source))
            card = review_cards.issue(
                db, source,
                '身份：B=梅芳\n冰冰袜：1700\n棉堆堆袜：2500\n小腿袜：300\n'
                '过膝袜：0\n女船袜：0\n男船袜：0\n合计：4500',
            )
            self.assertIn('老板代报：B=梅芳', card['card']['elements'][0]['text']['content'])


if __name__ == '__main__':
    unittest.main()
