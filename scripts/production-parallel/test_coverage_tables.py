import unittest

import coverage_tables
import missing_alerts


class CoverageTableTests(unittest.TestCase):
    def test_two_tables_are_fixed_summary_and_month_rows(self):
        text = coverage_tables.render(
            {
                "A": {"2026-09-20", "2026-09-21", "2026-09-30"},
                "B": {"2026-09-22", "2026-09-23", "2026-10-01"},
                "C": {"2026-09-20"},
                "D": {"2026-09-25"},
            },
            [(2026, 9), (2026, 10), (2026, 11), (2026, 12)],
        )
        self.assertEqual(text.count("### 一、人员总览"), 1)
        self.assertEqual(text.count("### 二、按月份查看生产日期"), 1)
        self.assertIn("| 人员 | 有效生产天数 | 已确认未上班 | 已报待查 | 待处理日期 | 最近有效生产日 | 当前状态 |", text)
        self.assertIn("| 月份 | A｜徐超超 | B｜梅芳 | C｜李鸿玉 | D｜张小翠 |", text)
        self.assertIn("| 2026年9月 |", text)
        self.assertIn("| 2026年10月 |", text)
        self.assertIn("尚无有效记录", text)
        self.assertNotIn("文件中没有有效记录的日期", text)

    def test_year_boundary_uses_full_year_labels(self):
        text = coverage_tables.render(
            {"A": {"2026-12-31", "2027-01-01"}},
            [(2026, 12), (2027, 1)],
        )
        self.assertIn("2026年12月31日–2027年1月1日", coverage_tables.compact_dates(
            ["2026-12-31", "2027-01-01"]
        ))
        self.assertIn("| 2027年1月 |", text)

    def test_attendance_explanation_matches_separate_status_columns_in_both_years(self):
        for year in (2026, 2027):
            with self.subTest(year=year):
                text = coverage_tables.render(
                    {'A': {f'{year}-01-01'}},
                    [(year, 1)],
                    attendance_status_map={
                        'A': {
                            f'{year}-01-02': 'not_worked',
                            f'{year}-01-03': 'already_reported',
                        },
                    },
                )
                self.assertIn('“已确认未上班”和“已报待查”两列分别统计对应的已确认状态天数', text)
                self.assertNotIn('“已确认出勤状态天数”列', text)
                self.assertIn(f'| A｜徐超超 | 1天 | 1天 | 1天 | 无 | {year}年1月1日 |', text)
                self.assertNotIn('✓ 1月2日', text)
                self.assertNotIn('✓ 1月3日', text)

    def test_fixed_rota_gap_is_marked_for_but_not_cd(self):
        dates = {
            "A": {"2026-09-20", "2026-09-22"},
            "B": {"2026-09-20", "2026-09-22"},
            "C": {"2026-09-20", "2026-09-22"},
            "D": {"2026-09-20"},
        }
        text = coverage_tables.render(dates, [(2026, 9)])
        self.assertIn("### 待核实日期", text)
        pending_rows = [
            line for line in text.splitlines()
            if line.startswith("|") and "尚无有效记录（待核实）" in line
        ]
        self.assertEqual(len(pending_rows), 3)
        self.assertIn("| D｜张小翠 | 1天 | 0天 | 0天 | 无 | 2026年9月20日 | 本周期只上班1天 |", text)

    def test_explicit_closed_window_can_include_trailing_gap(self):
        missing = coverage_tables.missing_dates(
            {"A": {"2026-09-20"}, "B": {"2026-09-20"}},
            coverage_tables.expected_dates(
                {"A": {"2026-09-20"}, "B": {"2026-09-20"}},
                window_start="2026-09-20", window_end="2026-09-22",
            ),
        )
        self.assertEqual(
            missing["A"],
            {coverage_tables._date("2026-09-21"), coverage_tables._date("2026-09-22")},
        )
        self.assertEqual(missing["C"], set())

    def test_backfill_reminder_has_one_copyable_block_per_date(self):
        text = coverage_tables.backfill_reminder("B", ["2026-09-20", "2026-09-21"])
        self.assertEqual(text.count("补报\nB=梅芳"), 2)
        self.assertIn("生产日期：2026-09-20", text)
        self.assertIn("生产日期：2026-09-21", text)
        self.assertIn("如果当天未上班，请回复：未上班：2026-09-20", text)

    def test_missing_alerts_are_one_per_date_and_exclude_cd(self):
        alerts = missing_alerts.build_alerts({
            "A": {"2026-09-20", "2026-09-22"},
            "B": {"2026-09-20", "2026-09-22"},
            "C": {"2026-09-20", "2026-09-22"},
            "D": {"2026-09-20"},
        })
        self.assertEqual([x["alert_key"] for x in alerts], [
            "A:2026-09-21", "B:2026-09-21", "C:2026-09-21",
        ])
        self.assertTrue(all("补报" in x["message"] for x in alerts))

    def test_cd_can_be_alerted_when_attendance_window_is_explicit(self):
        dates = {
            "A": {"2026-09-20"},
            "B": {"2026-09-20"},
            "C": {"2026-09-20"},
            "D": {"2026-09-20"},
        }
        expected = {code: {"2026-09-20", "2026-09-21"} for code in dates}
        alerts = missing_alerts.build_alerts(
            dates, expected=expected, workers=("C", "D")
        )
        self.assertEqual([x["alert_key"] for x in alerts], [
            "C:2026-09-21", "D:2026-09-21",
        ])
        self.assertIn("员工：C｜李鸿玉", alerts[0]["message"])

    def test_cd_explicit_expected_date_is_visible_in_table(self):
        text = coverage_tables.render(
            {"C": {"2026-09-20"}, "D": {"2026-09-20"}},
            [(2026, 9)],
            expected={
                "C": {"2026-09-20", "2026-09-21"},
                "D": {"2026-09-20", "2026-09-21"},
            },
        )
        self.assertIn("C｜李鸿玉 | 1天 | 0天 | 0天 | 9月21日 | 2026年9月20日", text)
        self.assertIn("D｜张小翠 | 1天 | 0天 | 0天 | 9月21日 | 2026年9月20日", text)

    def test_date_map_from_ledger_uses_detail_ids_only(self):
        import missing_alerts
        markdown = (
            "## A｜徐超超下机\n\n"
            "### 2026-09-20｜徐超超下机\n"
            "| 记录编号 | 产品 | 数量（双） | 状态 |\n"
            "|---|---|---:|---|\n"
            "| 20260920-A-001 | 棉堆堆袜 | 1 | 已确认 |\n"
            "## B｜梅芳下机\n"
            "| 20260922-B-001 | 棉堆堆袜 | 1 | 已确认 |\n"
            "## 人员生产记录覆盖情况\n"
            "| A｜徐超超 | 2026年9月20日 |\n"
        )
        self.assertEqual(missing_alerts.date_map_from_ledger(markdown), {
            "A": {"2026-09-20"}, "B": {"2026-09-22"}, "C": set(), "D": set(),
        })

    def test_backfill_dates_are_split_from_normal_dates_in_both_tables(self):
        text = coverage_tables.render(
            {"B": {"2026-09-20", "2026-09-21", "2026-09-22", "2026-09-23"}},
            [(2026, 9)],
            backfill_map={"B": {"2026-09-20", "2026-09-21"}},
        )
        self.assertIn("已确认（含补报）", text)
        self.assertIn("✓ 9月20日–21日（补报成功，2天）；✓ 9月22日–23日（2天）", text)

    def test_confirmed_non_working_day_is_status_only(self):
        text = coverage_tables.render(
            {"C": {"2026-09-20", "2026-10-01"}},
            [(2026, 9), (2026, 10)],
            not_worked_map={"C": {"2026-10-01"}},
        )
        self.assertIn("| C｜李鸿玉 | 1天 | 1天 | 0天 | 无 | 2026年9月20日 | 已确认（含未上班核实） |", text)
        self.assertIn("| 2026年10月 | 尚无有效记录 | 尚无有效记录 | 已确认未上班：10月1日 | 尚无有效记录 |", text)
        self.assertIn("| C｜李鸿玉 | 2026年10月1日 | 已确认未上班（不计入生产统计） |", text)
        self.assertNotIn("✓ 10月1日", text)


    def test_confirmed_status_suppresses_stale_pending_queue(self):
        text = coverage_tables.render(
            {'B': {'2026-10-01'}},
            [(2026, 10)],
            attendance_status_map={'B': {'2026-10-02': 'not_worked'}},
            pending_queue=[
                {'worker': 'B', 'production_date': '2026-10-02', 'state': 'sent'},
            ],
        )
        self.assertIn('已确认未上班：10月2日', text)
        self.assertNotIn('10月2日（核实卡已发送，等待回复）', text)
        self.assertNotIn('| B｜梅芳 | 2026年10月2日 | 核实卡已发送，等待回复 |', text)

    def test_confirmed_status_is_not_reintroduced_as_missing(self):
        text = coverage_tables.render(
            {'B': {'2026-09-20', '2026-10-03'},
             'C': {'2026-09-20', '2026-10-03'}},
            [(2026, 9), (2026, 10)],
            attendance_status_map={
                'B': {'2026-10-02': 'not_worked'},
                'C': {'2026-10-01': 'not_worked', '2026-10-02': 'not_worked'},
            },
        )
        self.assertNotIn('B｜梅芳 | 2026年10月2日 | 尚无有效记录（待核实）', text)
        self.assertNotIn('C｜李鸿玉 | 2026年10月1日 | 尚无有效记录（待核实）', text)
        self.assertNotIn('C｜李鸿玉 | 2026年10月2日 | 尚无有效记录（待核实）', text)

if __name__ == "__main__":
    unittest.main()
