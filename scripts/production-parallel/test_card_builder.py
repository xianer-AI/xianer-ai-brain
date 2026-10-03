import json
import unittest
import card_builder


class CardBuilderTests(unittest.TestCase):
    def test_unreported_product_is_rendered_as_zero_with_verification_note(self):
        card = card_builder.payload(
            '身份：B=梅芳 请核实\n'
            '过膝袜：核实\n'
            '合计：4000 双'
        )
        content = card['elements'][0]['text']['content']
        self.assertIn('过膝袜：0双（数量为0，请核实）', content)
        self.assertNotIn('过膝袜：核实\n', content)

    def test_builds_one_interactive_card_contract(self):
        card = card_builder.payload('身份：A=徐超超 请核实\n合计：4000 双')
        self.assertEqual(card_builder.CARD_PROTOCOL, 'CARD-INTERACTIVE-1')
        self.assertEqual(card['header']['title']['content'], '生产报数核对')
        self.assertEqual(card['elements'][0]['tag'], 'div')
        self.assertEqual(card['elements'][0]['text']['tag'], 'lark_md')
        self.assertIn('回复“**准确**”', card['elements'][0]['text']['content'])
        self.assertIn('回复“**错误**”', card['elements'][0]['text']['content'])
        self.assertIn('核实项按0双上传', card['elements'][0]['text']['content'])
        self.assertIn('直接重报完整、正确的六项生产数量', card['elements'][0]['text']['content'])

    def test_backfill_review_card_has_distinct_title(self):
        card = card_builder.payload('身份：B=梅芳 请核实\n合计：1500 双', backfill=True)
        self.assertEqual(card['header']['title']['content'], '生产补报数核对')

    def test_backfill_review_card_marks_date_and_type(self):
        card = card_builder.payload('身份：B=梅芳\n生产日：2026-09-20\n合计：1500 双', backfill=True)
        content = card['elements'][0]['text']['content']
        self.assertIn('生产日：**2026-09-20**', content)
        self.assertIn('记录类型：**补报**', content)

    def test_envelope_is_interactive_and_not_plain_text(self):
        message = card_builder.envelope('合计：4000 双')
        self.assertEqual(message['msg_type'], 'interactive')
        self.assertEqual(json.loads(message['content'])['elements'][0]['tag'], 'div')
        self.assertEqual(message['card_protocol'], card_builder.CARD_PROTOCOL)

    def test_receipt_payload_preserves_message_without_confirmation_instruction(self):
        message = ('已确认并上传成功。\n\n'
                   '• 员工：B（梅芳）\n'
                   '• GitHub commit：abc123')
        card = card_builder.receipt_payload(message)
        self.assertEqual(card['header']['title']['content'], '上传成功回执')
        self.assertEqual(card['header']['template'], 'blue')
        self.assertEqual(card['elements'][0]['text']['tag'], 'lark_md')
        self.assertEqual(card['elements'][0]['text']['content'], message)
        self.assertNotIn('请核实以上内容', card['elements'][0]['text']['content'])

    def test_backfill_receipt_has_distinct_title(self):
        card = card_builder.receipt_payload('• 记录类型：**补报**', backfill=True)
        self.assertEqual(card['header']['title']['content'], '补报上传成功回执')

    def test_receipt_payload_rejects_empty_message(self):
        with self.assertRaises(ValueError):
            card_builder.receipt_payload('  ')

    def test_backfill_payload_is_separate_from_confirmation_card(self):
        summary = ('【生产报数待核实】\n员工：B｜梅芳\n'
                   '生产日期：2026-09-20\n\n补报\nB=梅芳\n'
                   '生产日期：2026-09-20\n棉堆堆袜：___')
        card = card_builder.backfill_payload(summary)
        self.assertEqual(card['header']['title']['content'], '补发生产日期待核实')
        self.assertEqual(card['header']['template'], 'orange')
        content = card['elements'][0]['text']['content']
        self.assertIn('B=梅芳', content)
        self.assertNotIn('回复“**准确**”', content)

    def test_backfill_input_card_lists_pending_dates_but_processes_current_first(self):
        card = card_builder.backfill_input_payload(
            'B', '2026-09-20', pending_dates=['2026-09-20', '2026-09-21'])
        content = card['elements'][0]['content']
        self.assertIn('待补报日期共 **2 天**：2026-09-20、2026-09-21', content)
        self.assertIn('本次先处理当前日期；当前日期完成后，系统自动发送下一张。', content)
        self.assertEqual(content.count('生产日期：2026-09-20'), 1)


if __name__ == '__main__':
    unittest.main()
