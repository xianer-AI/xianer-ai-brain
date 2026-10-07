"""Display labels cannot change reports, stored bindings or copyable templates."""

import copy
import hashlib
import json
import re
import socket
import sqlite3
import subprocess
import unittest
from unittest.mock import patch

import backfill_flow
import card_builder
import commit_guard
import coverage_tables
import hermes_extract
import quantity_display
import review_cards
import service


class QuantityDisplayTests(unittest.TestCase):
    def setUp(self):
        # These checks exercise only pure rendering/parsing, with no live I/O.
        for target, name in ((sqlite3, 'connect'), (socket.socket, 'connect'),
                             (subprocess, 'run'), (subprocess, 'check_output')):
            guard = patch.object(target, name, side_effect=AssertionError('live I/O forbidden'))
            self.addCleanup(guard.stop)
            guard.start()

    def cases(self):
        for year in (2026, 2027):
            for worker, (name, process) in card_builder._WORKERS.items():
                yield year, worker, name, process, f'{year}-01-02'

    def test_review_render_does_not_mutate_stored_summary_digest_or_token(self):
        for year, worker, name, process, day in self.cases():
            for backfill in (False, True):
                with self.subTest(year=year, worker=worker, backfill=backfill):
                    summary = f'身份：{worker}={name} 请核实\n生产日：{day}\n棉堆堆袜：100 双\n合计：100 双'
                    row = {'summary': summary, 'digest': hashlib.sha256(summary.encode()).hexdigest(),
                           'token': 'existing-token', 'source': 'existing-source', 'backfill': backfill}
                    before = copy.deepcopy(row)
                    card = review_cards.card_payload(row)
                    self.assertEqual(row, before)
                    content = card['elements'][0]['text']['content']
                    note = quantity_display.quantity_note(worker)
                    original_content = card_builder.normalize_summary(summary)
                    if backfill:
                        original_content = card_builder._decorate_backfill_summary(original_content)
                    self.assertEqual(content, note + '\n\n' + original_content + '\n\n' + card_builder._INSTRUCTION)
                    self.assertEqual(card, review_cards.card_payload(row))

    def test_both_years_copy_template_and_added_explanation_keep_parse_identical(self):
        quantities = (100, 200, 30, 0, 0, 0)
        for year, worker, name, process, day in self.cases():
            with self.subTest(year=year, worker=worker):
                card = card_builder.backfill_input_payload(worker, day)
                content = card['elements'][0]['content']
                template = re.search(r'```\n(.*?)\n```', content, re.S).group(1)
                original = '\n'.join(['补报', f'{worker}={name}', f'生产日期：{day}',
                                      f'工序：{process}', *[f'{p}：' for p in card_builder._PRODUCTS]])
                self.assertEqual(template, original)
                filled = original
                for product, quantity in zip(card_builder._PRODUCTS, quantities):
                    filled = filled.replace(f'{product}：', f'{product}：{quantity}')
                event = {'messageId': 'offline-display-test', 'senderId': 'offline-worker',
                         'timestamp': day + 'T20:00:00+08:00', 'content': filled}
                note = quantity_display.quantity_note(worker)
                explanation = note + '\n说明：保留下面模板中的原工序，只填写六项数字。\n'
                if worker in ('A', 'B'):
                    self.assertNotIn('烤边', explanation)
                extended = dict(event, content=explanation + filled)
                self.assertEqual(service.fast_extract(event), service.fast_extract(extended))
                self.assertEqual(hermes_extract.deterministic_report(event),
                                 hermes_extract.deterministic_report(extended))
                parsed = hermes_extract.deterministic_report(extended)
                self.assertEqual(parsed['production_date'], day)
                self.assertEqual([x['quantity'] for x in parsed['items']], list(quantities))
                self.assertEqual({x['process'] for x in parsed['items']}, {process})

    def test_reminder_preserves_existing_copy_block(self):
        for year, worker, name, process, day in self.cases():
            with self.subTest(year=year, worker=worker):
                text = coverage_tables.backfill_reminder(worker, [day])
                self.assertIn(quantity_display.quantity_note(worker), text)
                template = '\n'.join(['补报', f'{worker}={name}', f'生产日期：{day}',
                                      *[f'{p}：___' for p in card_builder._PRODUCTS]])
                self.assertIn(template, text)

    def test_status_unknown_and_choice_cards_keep_no_quantity_label(self):
        for year, worker, name, process, day in self.cases():
            with self.subTest(year=year, worker=worker):
                for status in ('not_worked', 'already_reported'):
                    summary = f'身份：{worker}={name}\n生产日期：{day}\n出勤状态：{status}'
                    card = card_builder.status_payload(summary, status=status)
                    self.assertTrue(card['elements'][0]['text']['content'].startswith(summary + '\n\n'))
                    self.assertNotIn('数量口径', json.dumps(card, ensure_ascii=False))
                choice = backfill_flow.verification_card(worker, day)
                self.assertNotIn('数量口径', json.dumps(choice, ensure_ascii=False))
                self.assertEqual([x['value']['text'] for x in choice['elements'][1]['actions']],
                                 [f'生产补报 {number} {worker} {day}' for number in (1, 2, 3)])
        for summary in ('身份：待核实（A/B/C/D）\n合计：100 双',
                        '身份：A=徐超超\n身份：C=李鸿玉\n合计：100 双'):
            card = card_builder.payload(summary)
            self.assertEqual(card['elements'][0]['text']['content'],
                             summary + '\n\n' + card_builder._INSTRUCTION)

    def test_receipt_retains_process_and_skips_status_or_unknown_worker(self):
        for year, worker, name, process, day in self.cases():
            for status_only in (False, True):
                with self.subTest(year=year, worker=worker, status_only=status_only):
                    report = {'worker': worker, 'production_date': day, 'process': process,
                              'status_only': status_only, 'total': 100,
                              'items': [{'product': '棉堆堆袜', 'quantity': 100, 'process': process}]}
                    before = copy.deepcopy(report)
                    text = commit_guard.format_success_receipt(report, 'source', 'confirm', 'commit', 'reread')
                    self.assertEqual(report, before)
                    self.assertIn(f'• 工序：{process}', text)
                    if status_only:
                        self.assertNotIn('数量口径', text)
                        self.assertNotIn('• 棉堆堆袜：', text)
                    else:
                        self.assertIn(quantity_display.quantity_note(worker), text)
                        self.assertIn('• 合计：100 双', text)
        unknown = commit_guard.format_success_receipt(
            {'worker': 'unknown', 'production_date': '2027-01-02', 'items': []},
            'source', 'confirm', 'commit', 'reread')
        self.assertNotIn('数量口径', unknown)

    def test_proxy_and_historical_review_use_the_same_display_boundary(self):
        for prefix in ('身份', '老板代报', '员工代号'):
            summary = f'{prefix}：B=梅芳 请核实\n历史缺项补核\n合计：100 双'
            content = card_builder.payload(summary)['elements'][0]['text']['content']
            self.assertEqual(content, quantity_display.quantity_note('B') + '\n\n' +
                             summary + '\n\n' + card_builder._SUPPLEMENT_INSTRUCTION)


if __name__ == '__main__':
    unittest.main()
