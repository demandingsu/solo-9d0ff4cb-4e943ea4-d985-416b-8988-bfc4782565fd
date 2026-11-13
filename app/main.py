"""连锁便利店电价套餐比较 API + 电表校时整理 API。

入口一：POST /api/v1/compare-plans
流程：抓取报价站分页列表与套餐详情 -> 地区过滤 -> 资格校验
（缺费率 / 超已公布档位不得参与）-> 峰谷逐档计费 + 月基本费 -> 年度汇总排序。
任何列表或详情抓取失败都会记录失败地址，并把最低价结论标记为“待核实”。

入口二：POST /api/v1/calibrate-readings
流程：抓取供电商明细站分页（序号、带偏移的设备时间、电量、来源）->
同序号去重与冲突检测 -> 以锚点可信 UTC 时间计算偏差、相邻锚点间按序号
线性插值修正 -> 分段核验（锚点缺失 / 缺号 / 冲突 / 修正时间不递增的整段
待核定，不混入可信记录）。分页抓取失败时仍返回已采集记录与失败地址，
但整份结果待核定。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI

from .billing import annual_total, bill_plan, check_eligibility
from .calibration import Anchor, organize_readings
from .meter_scraper import crawl_readings
from .schemas import (
    AnchorOut,
    CalibrateRequest,
    CalibrateResponse,
    CompareRequest,
    CompareResponse,
    CrawlFailureOut,
    ExcludedPlanOut,
    MonthlyBillOut,
    PendingReasonOut,
    PendingRecordOut,
    PendingSegmentOut,
    RankedPlanOut,
    TierChargeOut,
    TrustedRecordOut,
)
from .scraper import crawl_quote_site

app = FastAPI(
    title="连锁便利店电价套餐比较 API",
    version="1.1.0",
    description="按门店峰谷用电预测比较电价套餐年度成本；并按校时锚点整理电表采样明细的真实时间。",
)


def _tier_out(charges) -> list[TierChargeOut]:
    return [TierChargeOut(**vars(charge)) for charge in charges]


def _monthly_out(bills) -> list[MonthlyBillOut]:
    return [
        MonthlyBillOut(
            month=bill.month,
            peak_kwh=bill.peak_kwh,
            valley_kwh=bill.valley_kwh,
            peak_charges=_tier_out(bill.peak_charges),
            valley_charges=_tier_out(bill.valley_charges),
            peak_energy_fee_cny=bill.peak_energy_fee_cny,
            valley_energy_fee_cny=bill.valley_energy_fee_cny,
            base_fee_cny=bill.base_fee_cny,
            monthly_total_cny=bill.monthly_total_cny,
        )
        for bill in bills
    ]


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/v1/compare-plans", response_model=CompareResponse)
def compare_plans(request: CompareRequest) -> CompareResponse:
    usage = [item.model_dump() for item in request.monthly_usage]
    crawl_result = crawl_quote_site(request.list_url, timeout=request.timeout_seconds)

    ranked: list[RankedPlanOut] = []
    excluded: list[ExcludedPlanOut] = []

    for plan in crawl_result.plans:
        eligibility = check_eligibility(plan, request.region, usage)
        if not eligibility.eligible:
            excluded.append(
                ExcludedPlanOut(
                    plan_id=plan.plan_id,
                    plan_name=plan.name,
                    supplier=plan.supplier,
                    regions=plan.regions,
                    reason=eligibility.reason or "unknown",
                    detail=eligibility.detail or "",
                    detail_url=plan.detail_url,
                    list_url=plan.list_url,
                )
            )
            continue
        bills = bill_plan(plan, usage)
        ranked.append(
            RankedPlanOut(
                rank=0,  # 排序后统一回填
                plan_id=plan.plan_id,
                plan_name=plan.name,
                supplier=plan.supplier,
                regions=plan.regions,
                annual_total_cny=annual_total(bills),
                detail_url=plan.detail_url,
                list_url=plan.list_url,
                monthly_bills=_monthly_out(bills),
            )
        )

    ranked.sort(key=lambda item: item.annual_total_cny)
    for index, item in enumerate(ranked, start=1):
        item.rank = index

    failures = [
        CrawlFailureOut(url=f.url, stage=f.stage, reason=f.reason, status=f.status)
        for f in crawl_result.failures
    ]

    has_failure = bool(failures)
    if ranked:
        cheapest = ranked[0]
        conclusion = {
            "lowest_plan_id": cheapest.plan_id,
            "lowest_plan_name": cheapest.plan_name,
            "lowest_annual_total_cny": cheapest.annual_total_cny,
            "verification_status": "pending_verification" if has_failure else "verified",
            "note": (
                "存在抓取失败的列表页或套餐详情，最低价结论待核实"
                if has_failure
                else "全部列表页与套餐详情抓取成功，最低价结论已核实"
            ),
        }
    else:
        conclusion = {
            "lowest_plan_id": None,
            "lowest_plan_name": None,
            "lowest_annual_total_cny": None,
            "verification_status": "pending_verification",
            "note": "没有可参与比较的有效套餐，无法给出最低价结论，待核实",
        }

    return CompareResponse(
        request_region=request.region,
        list_url=request.list_url,
        crawl={
            "visited_list_urls": crawl_result.visited_list_urls,
            "plans_crawled": len(crawl_result.plans),
            "eligible_count": len(ranked),
            "excluded_count": len(excluded),
            "failure_count": len(failures),
        },
        conclusion=conclusion,
        ranked_plans=ranked,
        excluded_plans=excluded,
        failures=failures,
    )


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@app.post("/api/v1/calibrate-readings", response_model=CalibrateResponse)
def calibrate_readings(request: CalibrateRequest) -> CalibrateResponse:
    crawl = crawl_readings(request.first_page_url, timeout=request.timeout_seconds)
    anchors = [Anchor(seq=item.seq, trusted_time=item.trusted_time_utc) for item in request.anchors]
    result = organize_readings(crawl.rows, anchors, crawl.visited_page_urls)

    failures = [
        CrawlFailureOut(url=f.url, stage=f.stage, reason=f.reason, status=f.status)
        for f in crawl.failures
    ]

    trusted = [
        TrustedRecordOut(
            seq=record.seq,
            device_time_utc=_iso_z(record.device_time),
            corrected_time_utc=_iso_z(record.corrected_time),
            offset_seconds=round(record.offset_seconds, 3),
            energy_kwh=record.energy_kwh,
            source_url=record.source_url,
        )
        for record in result.trusted_records
    ]
    pending_segments = [
        PendingSegmentOut(
            start_seq=segment.start_seq,
            end_seq=segment.end_seq,
            reasons=[
                PendingReasonOut(code=reason.code, detail=reason.detail, sources=reason.sources)
                for reason in segment.reasons
            ],
            records=[
                PendingRecordOut(
                    seq=record.seq,
                    device_time_utc=_iso_z(record.device_time_utc) if record.device_time_utc else None,
                    device_time_raw=record.device_time_raw,
                    energy_kwh=record.energy_kwh,
                    source_url=record.source_url,
                )
                for record in segment.records
            ],
        )
        for segment in result.pending_segments
    ]

    problems: list[str] = []
    if failures:
        problems.append(f"{len(failures)} 个分页抓取失败")
    if pending_segments:
        problems.append(f"{len(pending_segments)} 个序号段待核定")
    if problems:
        verification_status = "pending_verification"
        note = "；".join(problems) + "，待核定记录未混入可信记录"
    elif not trusted:
        verification_status = "pending_verification"
        note = "没有可校时的记录，无法给出可信结果，待核定"
    else:
        verification_status = "verified"
        note = "全部分页抓取成功，各锚点段校时核验通过，可信记录可直接用于峰谷时段核对"

    return CalibrateResponse(
        first_page_url=request.first_page_url,
        verification_status=verification_status,
        note=note,
        crawl={
            "visited_page_urls": crawl.visited_page_urls,
            "rows_collected": len(crawl.rows),
            "rows_skipped": crawl.rows_skipped,
            "unique_records": result.unique_records,
            "duplicates_skipped": result.duplicates_skipped,
            "conflict_seqs": result.conflict_seqs,
            "unparseable_seqs": result.unparseable_seqs,
            "failure_count": len(failures),
        },
        anchors=[
            AnchorOut(
                seq=outcome.seq,
                trusted_time_utc=_iso_z(outcome.trusted_time),
                device_time_utc=_iso_z(outcome.device_time) if outcome.device_time else None,
                offset_seconds=(
                    round(outcome.offset_seconds, 3) if outcome.offset_seconds is not None else None
                ),
                status=outcome.status,
                source_url=outcome.source_url,
            )
            for outcome in result.anchors
        ],
        trusted_records=trusted,
        pending_segments=pending_segments,
        failures=failures,
    )
