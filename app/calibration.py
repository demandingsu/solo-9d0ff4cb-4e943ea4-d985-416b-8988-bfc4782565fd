"""电表时钟校时整理核心。

输入：分页抓取的采样行（序号、带偏移的设备时间、电量、来源）与校时锚点
（采样序号 + 可信 UTC 时间）。

整理规则：
1. 同序号且内容一致的记录只计一次（跨页重复去重）；同序号内容不一致
   视为记录冲突。
2. 锚点处偏差 = 可信 UTC 时间 - 该序号设备时间；相邻锚点之间按序号对偏差
   做线性插值，每条记录的修正时间 = 设备时间 + 插值偏差。
3. 以下任一情况发生时，整段（相邻两锚点之间的序号区间）待核定，段内记录
   不得混入可信记录，并列出原因与来源：
   - 锚点序号的记录缺失、冲突或无法解析（anchor_missing）；
   - 段内缺号（sequence_gap）；
   - 段内记录冲突（record_conflict）或无法解析（record_unparseable）；
   - 修正时间未随序号严格递增（non_monotonic_corrected_time）。
4. 锚点覆盖范围之外的记录无法插值校时，同样整段待核定
   （anchor_coverage_missing）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .meter_scraper import MeterRow

# 待核定原因代码
ANCHOR_MISSING = "anchor_missing"
SEQUENCE_GAP = "sequence_gap"
RECORD_CONFLICT = "record_conflict"
RECORD_UNPARSEABLE = "record_unparseable"
NON_MONOTONIC = "non_monotonic_corrected_time"
ANCHOR_COVERAGE_MISSING = "anchor_coverage_missing"


@dataclass
class Anchor:
    seq: int
    trusted_time: datetime  # 需为带时区时间，内部统一按 UTC 处理


@dataclass
class ParsedRecord:
    seq: int
    device_time: datetime  # 已规范为 UTC 的设备时间
    energy_kwh: float
    source_url: str


@dataclass
class PendingReason:
    code: str
    detail: str
    sources: list[str] = field(default_factory=list)


@dataclass
class PendingRecord:
    seq: int
    device_time_utc: datetime | None  # 无法解析时为 None
    device_time_raw: str  # 页面原始字符串，便于人工复核
    energy_kwh: float | None
    source_url: str


@dataclass
class PendingSegment:
    start_seq: int
    end_seq: int
    reasons: list[PendingReason]
    records: list[PendingRecord]


@dataclass
class TrustedRecord:
    seq: int
    device_time: datetime
    corrected_time: datetime
    offset_seconds: float
    energy_kwh: float
    source_url: str


@dataclass
class AnchorOutcome:
    seq: int
    trusted_time: datetime
    status: str  # applied | missing | conflict | unparseable
    device_time: datetime | None
    offset_seconds: float | None
    source_url: str | None


@dataclass
class CalibrationResult:
    anchors: list[AnchorOutcome]
    trusted_records: list[TrustedRecord]
    pending_segments: list[PendingSegment]
    unique_records: int  # 去重后可用的唯一记录数
    duplicates_skipped: int  # 同序号同内容被跳过的重复行数
    conflict_seqs: list[int]
    unparseable_seqs: list[int]


def _parse_device_time(raw: str) -> datetime | None:
    """解析设备时间，要求 ISO 8601 且携带时区，统一转为 UTC。"""
    text = raw.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None  # 不带时区的时间无法与可信 UTC 对齐，视为无法解析
    return parsed.astimezone(timezone.utc)


def _parse_energy(raw: str) -> float | None:
    text = raw.strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


@dataclass
class _SeqGroup:
    """同一序号下采集到的全部行及其解析结果。"""

    seq: int
    rows: list[MeterRow] = field(default_factory=list)
    device_times: list[datetime | None] = field(default_factory=list)
    energies: list[float | None] = field(default_factory=list)

    @property
    def has_broken(self) -> bool:
        return any(t is None or e is None for t, e in zip(self.device_times, self.energies))

    @property
    def has_conflict(self) -> bool:
        distinct = {
            (t, e)
            for t, e in zip(self.device_times, self.energies)
            if t is not None and e is not None
        }
        return len(distinct) > 1

    @property
    def ok_record(self) -> ParsedRecord | None:
        """全部行解析成功且内容一致时，返回去重后的唯一记录。"""
        if not self.rows or self.has_broken or self.has_conflict:
            return None
        return ParsedRecord(
            seq=self.seq,
            device_time=self.device_times[0],
            energy_kwh=self.energies[0],
            source_url=self.rows[0].source_url,
        )

    @property
    def sources(self) -> list[str]:
        seen: list[str] = []
        for row in self.rows:
            if row.source_url not in seen:
                seen.append(row.source_url)
        return seen

    def pending_records(self) -> list[PendingRecord]:
        """按原始内容去重后展开为待核定记录（冲突时每个版本各列一条）。"""
        result: list[PendingRecord] = []
        seen_variants: set[tuple[str, str]] = set()
        for row, device_time, energy in zip(self.rows, self.device_times, self.energies):
            variant = (row.device_time_raw, row.energy_raw)
            if variant in seen_variants:
                continue
            seen_variants.add(variant)
            result.append(
                PendingRecord(
                    seq=self.seq,
                    device_time_utc=device_time,
                    device_time_raw=row.device_time_raw,
                    energy_kwh=energy,
                    source_url=row.source_url,
                )
            )
        return result


def _build_groups(rows: list[MeterRow]) -> dict[int, _SeqGroup]:
    groups: dict[int, _SeqGroup] = {}
    for row in rows:
        group = groups.setdefault(row.seq, _SeqGroup(seq=row.seq))
        group.rows.append(row)
        group.device_times.append(_parse_device_time(row.device_time_raw))
        group.energies.append(_parse_energy(row.energy_raw))
    return groups


def _resolve_anchor(anchor: Anchor, groups: dict[int, _SeqGroup]) -> AnchorOutcome:
    trusted = anchor.trusted_time.astimezone(timezone.utc)
    group = groups.get(anchor.seq)
    if group is None:
        return AnchorOutcome(anchor.seq, trusted, "missing", None, None, None)
    record = group.ok_record
    if record is not None:
        offset = (trusted - record.device_time).total_seconds()
        return AnchorOutcome(anchor.seq, trusted, "applied", record.device_time, offset, record.source_url)
    status = "conflict" if group.has_conflict else "unparseable"
    return AnchorOutcome(anchor.seq, trusted, status, None, None, group.rows[0].source_url)


def _interpolate_offset(seq: int, lo_seq: int, hi_seq: int, lo_offset: float, hi_offset: float) -> float:
    """按序号在相邻锚点间对偏差做线性插值。"""
    ratio = (seq - lo_seq) / (hi_seq - lo_seq)
    return lo_offset + ratio * (hi_offset - lo_offset)


def _segment_pending_records(groups: dict[int, _SeqGroup], seqs: range) -> list[PendingRecord]:
    records: list[PendingRecord] = []
    for seq in seqs:
        group = groups.get(seq)
        if group is not None:
            records.extend(group.pending_records())
    return records


def organize_readings(
    rows: list[MeterRow],
    anchors: list[Anchor],
    visited_page_urls: list[str] | None = None,
) -> CalibrationResult:
    """整理采样行：去重、冲突检测、锚点校时、分段判定。

    anchors 需至少两个且序号互不相同（请求层已校验，此处防御性复核）。
    """
    if len(anchors) < 2:
        raise ValueError("校时锚点至少需要两个")
    anchor_seqs = [anchor.seq for anchor in anchors]
    if len(set(anchor_seqs)) != len(anchor_seqs):
        raise ValueError("校时锚点的采样序号必须互不相同")

    page_sources = list(visited_page_urls or [])
    groups = _build_groups(rows)

    duplicates_skipped = 0
    conflict_seqs: list[int] = []
    unparseable_seqs: list[int] = []
    ok_records: dict[int, ParsedRecord] = {}
    for seq, group in groups.items():
        record = group.ok_record
        if record is not None:
            ok_records[seq] = record
            duplicates_skipped += len(group.rows) - 1
        if group.has_conflict:
            conflict_seqs.append(seq)
        if group.has_broken:
            unparseable_seqs.append(seq)

    ordered_anchors = sorted(anchors, key=lambda a: a.seq)
    outcomes = [_resolve_anchor(anchor, groups) for anchor in ordered_anchors]

    trusted: list[TrustedRecord] = []
    pending: list[PendingSegment] = []

    for index in range(len(ordered_anchors) - 1):
        lo, hi = outcomes[index], outcomes[index + 1]
        # 段内序号区间：首段含左锚点，其余段左开右闭，保证每条记录只属于一段。
        inner = range(lo.seq if index == 0 else lo.seq + 1, hi.seq + 1)
        reasons: list[PendingReason] = []

        for outcome in (lo, hi):
            if outcome.status != "applied":
                group = groups.get(outcome.seq)
                reasons.append(
                    PendingReason(
                        code=ANCHOR_MISSING,
                        detail=(
                            f"锚点序号 {outcome.seq} 的采样记录"
                            f"{'缺失' if outcome.status == 'missing' else '存在冲突' if outcome.status == 'conflict' else '无法解析'}"
                            "，无法确定该锚点偏差"
                        ),
                        sources=group.sources if group is not None else page_sources,
                    )
                )

        for seq in inner:
            group = groups.get(seq)
            if group is None:
                continue
            if group.has_conflict:
                reasons.append(
                    PendingReason(
                        code=RECORD_CONFLICT,
                        detail=f"序号 {seq} 存在内容不一致的重复记录，无法取舍",
                        sources=group.sources,
                    )
                )
            if group.has_broken:
                reasons.append(
                    PendingReason(
                        code=RECORD_UNPARSEABLE,
                        detail=f"序号 {seq} 的设备时间或电量无法解析",
                        sources=group.sources,
                    )
                )

        missing = [seq for seq in inner if seq not in groups]
        if missing:
            reasons.append(
                PendingReason(
                    code=SEQUENCE_GAP,
                    detail="段内缺号：" + "、".join(str(seq) for seq in missing),
                    sources=page_sources,
                )
            )

        if not reasons:
            # 两个锚点均已应用且段内无缺失：计算修正时间并校验严格递增。
            corrected: dict[int, datetime] = {}
            for seq in range(lo.seq, hi.seq + 1):
                record = ok_records[seq]
                offset = _interpolate_offset(seq, lo.seq, hi.seq, lo.offset_seconds, hi.offset_seconds)
                corrected[seq] = record.device_time + timedelta(seconds=offset)
            inversion = next(
                (
                    seq
                    for seq in range(lo.seq, hi.seq)
                    if corrected[seq + 1] <= corrected[seq]
                ),
                None,
            )
            if inversion is not None:
                reasons.append(
                    PendingReason(
                        code=NON_MONOTONIC,
                        detail=(
                            f"修正时间未随序号递增：序号 {inversion} 为 "
                            f"{corrected[inversion].isoformat()}，序号 {inversion + 1} 为 "
                            f"{corrected[inversion + 1].isoformat()}"
                        ),
                        sources=sorted(
                            {ok_records[inversion].source_url, ok_records[inversion + 1].source_url}
                        ),
                    )
                )
            else:
                for seq in inner:
                    record = ok_records[seq]
                    offset = _interpolate_offset(
                        seq, lo.seq, hi.seq, lo.offset_seconds, hi.offset_seconds
                    )
                    trusted.append(
                        TrustedRecord(
                            seq=seq,
                            device_time=record.device_time,
                            corrected_time=record.device_time + timedelta(seconds=offset),
                            offset_seconds=offset,
                            energy_kwh=record.energy_kwh,
                            source_url=record.source_url,
                        )
                    )

        if reasons:
            pending.append(
                PendingSegment(
                    start_seq=lo.seq,
                    end_seq=hi.seq,
                    reasons=reasons,
                    records=_segment_pending_records(groups, inner),
                )
            )

    # 锚点覆盖范围之外的记录：无法插值校时，整段待核定。
    first_seq, last_seq = ordered_anchors[0].seq, ordered_anchors[-1].seq
    below = sorted(seq for seq in groups if seq < first_seq)
    above = sorted(seq for seq in groups if seq > last_seq)
    for seq_list, detail in (
        (below, f"序号小于首个锚点 {first_seq}，缺少两侧锚点，无法插值校时"),
        (above, f"序号大于末个锚点 {last_seq}，缺少两侧锚点，无法插值校时"),
    ):
        if not seq_list:
            continue
        reasons = [
            PendingReason(
                code=ANCHOR_COVERAGE_MISSING,
                detail=detail,
                sources=page_sources,
            )
        ]
        for seq in seq_list:
            group = groups[seq]
            if group.has_conflict:
                reasons.append(
                    PendingReason(
                        code=RECORD_CONFLICT,
                        detail=f"序号 {seq} 存在内容不一致的重复记录，无法取舍",
                        sources=group.sources,
                    )
                )
            if group.has_broken:
                reasons.append(
                    PendingReason(
                        code=RECORD_UNPARSEABLE,
                        detail=f"序号 {seq} 的设备时间或电量无法解析",
                        sources=group.sources,
                    )
                )
        pending.append(
            PendingSegment(
                start_seq=seq_list[0],
                end_seq=seq_list[-1],
                reasons=reasons,
                records=_segment_pending_records(groups, range(seq_list[0], seq_list[-1] + 1)),
            )
        )

    trusted.sort(key=lambda record: record.seq)
    pending.sort(key=lambda segment: segment.start_seq)

    return CalibrationResult(
        anchors=outcomes,
        trusted_records=trusted,
        pending_segments=pending,
        unique_records=len(ok_records),
        duplicates_skipped=duplicates_skipped,
        conflict_seqs=sorted(conflict_seqs),
        unparseable_seqs=sorted(unparseable_seqs),
    )
