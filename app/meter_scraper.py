"""电表明细站抓取与解析：分页采样明细。

页面结构约定（见 ../meter-site 本地样例站）：
- 每页 main[data-page="meter-readings"] 为明细主容器
- 明细行 tr[data-reading] 携带数据属性：
  data-seq（采样序号）、data-device-time（带偏移的设备时间，ISO 8601）、
  data-energy-kwh（电量，千瓦时）
- 分页：nav[data-pagination] 内 a[rel=next]

抓取失败（连接错误、HTTP 4xx/5xx、页面结构无法解析）归一化为 CrawlFailure，
已采集的行仍然保留，由上层决定整体待核定。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .fetcher import FetchError, fetch_url, join_url
from .html_dom import parse_html
from .scraper import CrawlFailure

MAX_READING_PAGES = 50  # 分页安全上限，防止异常站点造成无限翻页


@dataclass
class MeterRow:
    """一页上的一行采样明细。设备时间与电量保留页面原始字符串，

    解析与校验放到校时整理阶段，以便把无法解析的行归入对应序号段待核定。
    """

    seq: int
    device_time_raw: str
    energy_raw: str
    source_url: str  # 该行所在的分页地址（来源）


@dataclass
class MeterCrawlResult:
    rows: list[MeterRow] = field(default_factory=list)
    failures: list[CrawlFailure] = field(default_factory=list)
    visited_page_urls: list[str] = field(default_factory=list)
    rows_skipped: int = 0  # 缺少合法序号、无法归属的行数


def parse_reading_page(markup: str, page_url: str) -> tuple[list[MeterRow], str | None, int]:
    """解析一页明细，返回（明细行, 下一页地址, 跳过行数）。"""
    root = parse_html(markup)
    main = root.find(tag="main", attr="data-page", attr_value="meter-readings")
    if main is None:
        raise ValueError("页面缺少 meter-readings 主容器")

    rows: list[MeterRow] = []
    skipped = 0
    for tr in main.find_all(tag="tr", attr="data-reading"):
        seq_raw = (tr.get("data-seq") or "").strip()
        try:
            seq = int(seq_raw)
        except ValueError:
            # 序号都无法解析的行无法归属到任何序号段，只能跳过并计数。
            skipped += 1
            continue
        rows.append(
            MeterRow(
                seq=seq,
                device_time_raw=(tr.get("data-device-time") or "").strip(),
                energy_raw=(tr.get("data-energy-kwh") or "").strip(),
                source_url=page_url,
            )
        )

    next_link = root.find(tag="a", attr="rel", attr_value="next")
    next_url = (
        join_url(page_url, next_link.get("href"))
        if next_link is not None and next_link.get("href")
        else None
    )
    return rows, next_url, skipped


def crawl_readings(first_page_url: str, timeout: float = 8.0) -> MeterCrawlResult:
    """从明细首页开始跟随 next 分页抓取全部采样行。

    任一分页抓取失败即停止翻页，已采集的行与失败地址一并返回。
    """
    result = MeterCrawlResult()
    seen: set[str] = set()
    current: str | None = first_page_url

    while current and current not in seen and len(seen) < MAX_READING_PAGES:
        seen.add(current)
        result.visited_page_urls.append(current)
        try:
            page = fetch_url(current, timeout=timeout)
        except FetchError as exc:
            result.failures.append(
                CrawlFailure(url=current, stage="list", reason=exc.reason, status=exc.status)
            )
            break
        try:
            rows, next_url, skipped = parse_reading_page(page.text, page.url)
        except ValueError as exc:
            result.failures.append(CrawlFailure(url=current, stage="list", reason=str(exc)))
            break
        result.rows.extend(rows)
        result.rows_skipped += skipped
        current = next_url

    return result
