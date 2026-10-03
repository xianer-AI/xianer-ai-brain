import unittest

from hermes_extract import deterministic_report


class DeterministicReportTests(unittest.TestCase):
    def test_employee_prefix_does_not_hide_first_product(self):
        report = deterministic_report({
            'senderId': 'ou_owner',
            'content': '梅芳发的冰冰袜1700双\n棉堆堆袜2500双\n小腿袜300双\n过膝袜0\n女船袜0\n男船袜0',
            'timestamp': '1790475527724',
        })
        items = {item['product']: item['quantity'] for item in report['items']}
        self.assertEqual(report['worker'], 'B')
        self.assertEqual(items['冰冰袜'], 1700)
        self.assertEqual(sum(items.values()), 4500)
        self.assertEqual(report['missing'], [])

    def test_explicit_owner_proxy_report_is_marked_as_proxy(self):
        report = deterministic_report({
            'senderId': 'ou_owner',
            'content': '代报：B=梅芳\n冰冰袜1700双\n棉堆堆袜2500双\n小腿袜300双\n过膝袜0\n女船袜0\n男船袜0',
            'timestamp': '1790475527724',
        })
        self.assertEqual(report['worker'], 'B')
        self.assertTrue(report['proxy'])


if __name__ == '__main__':
    unittest.main()
