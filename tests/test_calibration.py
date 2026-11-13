"""电表校时整理的离线测试：抓取随仓库提供的本地明细站（file://），
并用合成数据覆盖冲突、锚点缺失、缺号、修正时间不递增、抓取失败等场景。

运行（在仓库根目录）：
    python3 -I -m unittest discover -s tests -v
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.calibration import (
    ANCHOR_COVERAGE_MISSING,
    ANCHOR_MISSING,
    NON_MONOTONIC,
    RECORD_CONFLICT,
    RECORD_UNPARSEABLE,
    SEQUENCE_GAP,
    Anchor,
    organize_readings,
)
from app.meter_scraper import MeterRow, crawl_readings

ROOT = Path(__file__).resolve().parent.parent
FIRST_PAGE_URL = (ROOT / "meter-site" / "index.html").as_uri()

# 演示数据的真实采样时刻：2026-10-01T00:00:00Z 起每 15 分钟一条；
# 设备时钟初始快 300 秒，且每个序号再漂移 20 秒。
BASE = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
ANCHORS = [
    Anchor(seq=1, trusted_time=BASE),
    Anchor(seq=12, trusted_time=datetime(2026, 10, 1, 2, 45, tzinfo=timezone.utc)),
    Anchor(seq=20, trusted_time=datetime(2026, 10, 1, 4, 45, tzinfo=timezone.utc)),
]


def make_row(seq: int, device_time: str, energy: str, source: str = "test://page-1") -> MeterRow:
    return MeterRow(seq=seq, device_time_raw=device_time, energy_raw=energy, source_url=source)


class MeterCrawlTest(unittest.TestCase):
    def test_crawls_all_pages_and_collects_rows(self) -> None:
        result = crawl_readings(FIRST_PAGE_URL)
        self.assertEqual(len(result.visited_page_urls), 3, "应跟随 next 链接抓取三页明细")
        self.assertEqual(result.visited_page_urls[0], FIRST_PAGE_URL)
        self.assertEqual(result.failures, [])
        # 三页分别含 8、8、5 行（第 2 页与第 1 页重叠 2 行）
        self.assertEqual(len(result.rows), 21)
        self.assertEqual(result.rows_skipped, 0)
        first = result.rows[0]
        self.assertEqual(first.seq, 1)
        self.assertEqual(first.device_time_raw, "2026-10-01T00:05:00Z")
        self.assertEqual(first.energy_raw, "3.2")
        self.assertEqual(first.source_url, FIRST_PAGE_URL)

    def test_page_failure_keeps_collected_rows(self) -> None:
        # 临时构造两页：第 1 页正常，next 指向不存在的文件 -> 抓取失败但保留已采集记录
        with tempfile.TemporaryDirectory() as tmp:
            page1 = Path(tmp) / "page1.html"
            page1.write_text(
                """<html><body><main data-page="meter-readings">
<table><tbody>
<tr data-reading data-seq="1" data-device-time="2026-10-01T00:05:00Z" data-energy-kwh="3.2"></tr>
<tr data-reading data-seq="2" data-device-time="2026-10-01T00:20:20Z" data-energy-kwh="3.4"></tr>
</tbody></table>
<nav data-pagination><a rel="next" href="missing.html">next</a></nav>
</main></body></html>""",
                encoding="utf-8",
            )
            result = crawl_readings(page1.as_uri())
        self.assertEqual(len(result.rows), 2, "失败前已采集的行应保留")
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.failures[0].status, 404)
        self.assertTrue(result.failures[0].url.endswith("missing.html"))


class CalibrationDemoSiteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.crawl = crawl_readings(FIRST_PAGE_URL)
        self.result = organize_readings(self.crawl.rows, ANCHORS, self.crawl.visited_page_urls)

    def test_cross_page_duplicates_counted_once(self) -> None:
        self.assertEqual(self.result.duplicates_skipped, 2, "跨页重复的序号 7、8 只计一次")
        self.assertEqual(self.result.unique_records, 19)
        trusted_seqs = [record.seq for record in self.result.trusted_records]
        self.assertEqual(len(trusted_seqs), len(set(trusted_seqs)), "可信记录不得有重复序号")

    def test_anchor_offsets_from_device_time(self) -> None:
        outcomes = {anchor.seq: anchor for anchor in self.result.anchors}
        self.assertEqual(outcomes[1].status, "applied")
        self.assertEqual(outcomes[1].offset_seconds, -300.0)
        self.assertEqual(outcomes[12].offset_seconds, -520.0)
        self.assertEqual(outcomes[20].offset_seconds, -680.0)

    def test_verified_segment_interpolates_drift(self) -> None:
        trusted = {record.seq: record for record in self.result.trusted_records}
        self.assertEqual(sorted(trusted), list(range(1, 13)), "序号 1-12 段应全部可信")
        # 漂移为线性，插值修正后应精确回到 15 分钟整点
        self.assertEqual(trusted[1].corrected_time, BASE)
        self.assertEqual(trusted[2].corrected_time, datetime(2026, 10, 1, 0, 15, tzinfo=timezone.utc))
        self.assertEqual(trusted[2].offset_seconds, -320.0)
        self.assertEqual(trusted[12].corrected_time, datetime(2026, 10, 1, 2, 45, tzinfo=timezone.utc))
        self.assertEqual(trusted[12].offset_seconds, -520.0)
        # 修正时间在段内严格递增
        corrected = [trusted[seq].corrected_time for seq in sorted(trusted)]
        self.assertEqual(corrected, sorted(corrected))
        self.assertEqual(len(set(corrected)), len(corrected))

    def test_gap_segment_pending_and_not_mixed_into_trusted(self) -> None:
        trusted_seqs = {record.seq for record in self.result.trusted_records}
        self.assertEqual(len(self.result.pending_segments), 1)
        segment = self.result.pending_segments[0]
        self.assertEqual((segment.start_seq, segment.end_seq), (12, 20))
        gap_reasons = [reason for reason in segment.reasons if reason.code == SEQUENCE_GAP]
        self.assertEqual(len(gap_reasons), 1)
        self.assertIn("17", gap_reasons[0].detail)
        self.assertTrue(gap_reasons[0].sources, "缺号原因应列出来源分页")
        pending_seqs = {record.seq for record in segment.records}
        self.assertEqual(pending_seqs, {13, 14, 15, 16, 18, 19, 20})
        self.assertTrue(pending_seqs.isdisjoint(trusted_seqs), "待核定段记录不得混入可信记录")


class CalibrationSyntheticTest(unittest.TestCase):
    def test_conflict_marks_whole_segment_pending(self) -> None:
        rows = [
            make_row(1, "2026-10-01T00:05:00Z", "3.2"),
            make_row(2, "2026-10-01T00:20:20Z", "3.4", source="test://page-1"),
            make_row(2, "2026-10-01T00:20:20Z", "9.9", source="test://page-2"),  # 冲突：电量不一致
            make_row(3, "2026-10-01T00:35:40Z", "3.1"),
            make_row(4, "2026-10-01T00:51:00Z", "2.8"),
        ]
        anchors = [
            Anchor(seq=1, trusted_time=BASE),
            Anchor(seq=4, trusted_time=datetime(2026, 10, 1, 0, 45, tzinfo=timezone.utc)),
        ]
        result = organize_readings(rows, anchors, ["test://page-1", "test://page-2"])
        self.assertEqual(result.trusted_records, [])
        self.assertEqual(result.conflict_seqs, [2])
        self.assertEqual(len(result.pending_segments), 1)
        segment = result.pending_segments[0]
        conflict = [r for r in segment.reasons if r.code == RECORD_CONFLICT]
        self.assertEqual(len(conflict), 1)
        self.assertIn("test://page-2", conflict[0].sources)
        # 冲突的两个版本都应列入待核定记录
        seq2_records = [r for r in segment.records if r.seq == 2]
        self.assertEqual(len(seq2_records), 2)

    def test_anchor_record_missing_marks_adjacent_segments_pending(self) -> None:
        rows = [
            make_row(1, "2026-10-01T00:05:00Z", "3.2"),
            make_row(2, "2026-10-01T00:20:20Z", "3.4"),
            make_row(3, "2026-10-01T00:35:40Z", "3.1"),
        ]
        anchors = [
            Anchor(seq=1, trusted_time=BASE),
            Anchor(seq=2, trusted_time=datetime(2026, 10, 1, 0, 15, tzinfo=timezone.utc)),
            Anchor(seq=4, trusted_time=datetime(2026, 10, 1, 0, 45, tzinfo=timezone.utc)),  # 无此记录
        ]
        result = organize_readings(rows, anchors, [])
        outcomes = {anchor.seq: anchor.status for anchor in result.anchors}
        self.assertEqual(outcomes[4], "missing")
        self.assertEqual(len(result.pending_segments), 1, "仅锚点 4 所在的段待核定")
        segment = result.pending_segments[0]
        self.assertEqual((segment.start_seq, segment.end_seq), (2, 4))
        self.assertEqual(segment.reasons[0].code, ANCHOR_MISSING)
        # 与缺失锚点无关的段仍可信
        self.assertEqual([record.seq for record in result.trusted_records], [1, 2])

    def test_unparseable_row_marks_segment_pending(self) -> None:
        rows = [
            make_row(1, "2026-10-01T00:05:00Z", "3.2"),
            make_row(2, "设备时钟故障", "3.4"),  # 设备时间无法解析
            make_row(3, "2026-10-01T00:35:40Z", "3.1"),
        ]
        anchors = [
            Anchor(seq=1, trusted_time=BASE),
            Anchor(seq=3, trusted_time=datetime(2026, 10, 1, 0, 30, tzinfo=timezone.utc)),
        ]
        result = organize_readings(rows, anchors, [])
        self.assertEqual(result.unparseable_seqs, [2])
        self.assertEqual(result.trusted_records, [])
        codes = {reason.code for reason in result.pending_segments[0].reasons}
        self.assertIn(RECORD_UNPARSEABLE, codes)

    def test_non_monotonic_corrected_time_marks_segment_pending(self) -> None:
        rows = [
            make_row(1, "2026-10-01T00:05:00Z", "3.2"),
            make_row(2, "2026-10-01T00:20:00Z", "3.4"),
            make_row(3, "2026-10-01T00:10:00Z", "3.1"),  # 设备时钟回跳
            make_row(4, "2026-10-01T00:50:00Z", "2.8"),
        ]
        anchors = [
            Anchor(seq=1, trusted_time=BASE),
            Anchor(seq=4, trusted_time=datetime(2026, 10, 1, 0, 45, tzinfo=timezone.utc)),
        ]
        result = organize_readings(rows, anchors, [])
        self.assertEqual(result.trusted_records, [])
        codes = {reason.code for reason in result.pending_segments[0].reasons}
        self.assertIn(NON_MONOTONIC, codes)

    def test_records_outside_anchor_coverage_pending(self) -> None:
        rows = [
            make_row(1, "2026-10-01T00:05:00Z", "3.2"),
            make_row(2, "2026-10-01T00:20:20Z", "3.4"),
            make_row(3, "2026-10-01T00:35:40Z", "3.1"),
            make_row(4, "2026-10-01T00:51:00Z", "2.8"),
            make_row(5, "2026-10-01T01:06:20Z", "2.6"),
        ]
        anchors = [
            Anchor(seq=2, trusted_time=datetime(2026, 10, 1, 0, 15, tzinfo=timezone.utc)),
            Anchor(seq=4, trusted_time=datetime(2026, 10, 1, 0, 45, tzinfo=timezone.utc)),
        ]
        result = organize_readings(rows, anchors, [])
        self.assertEqual([record.seq for record in result.trusted_records], [2, 3, 4])
        self.assertEqual(len(result.pending_segments), 2, "锚点范围之外上下各一段待核定")
        for segment in result.pending_segments:
            self.assertEqual(segment.reasons[0].code, ANCHOR_COVERAGE_MISSING)
        self.assertEqual([r.seq for r in result.pending_segments[0].records], [1])
        self.assertEqual([r.seq for r in result.pending_segments[1].records], [5])

    def test_identical_duplicates_do_not_conflict(self) -> None:
        rows = [
            make_row(1, "2026-10-01T00:05:00Z", "3.2", source="test://page-1"),
            make_row(1, "2026-10-01T00:05:00Z", "3.2", source="test://page-2"),  # 跨页同内容重复
            make_row(2, "2026-10-01T00:20:20Z", "3.4"),
        ]
        anchors = [
            Anchor(seq=1, trusted_time=BASE),
            Anchor(seq=2, trusted_time=datetime(2026, 10, 1, 0, 15, tzinfo=timezone.utc)),
        ]
        result = organize_readings(rows, anchors, [])
        self.assertEqual(result.duplicates_skipped, 1)
        self.assertEqual(result.conflict_seqs, [])
        self.assertEqual(len(result.trusted_records), 2)
        self.assertEqual(result.pending_segments, [])


if __name__ == "__main__":
    unittest.main()
