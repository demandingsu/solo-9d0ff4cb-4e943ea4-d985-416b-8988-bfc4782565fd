"""API 请求 / 响应模型（Pydantic v2）。"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator


class MonthlyUsage(BaseModel):
    month: int = Field(ge=1, le=12, description="月份，1-12")
    peak_kwh: float = Field(ge=0, description="当月峰段用电量（千瓦时）")
    valley_kwh: float = Field(ge=0, description="当月谷段用电量（千瓦时）")


class CompareRequest(BaseModel):
    list_url: str = Field(min_length=1, description="报价站套餐列表页 URL，支持 http/https/file")
    region: str = Field(min_length=1, description="门店所在地区，需与套餐页地区标签匹配，如“上海”")
    monthly_usage: list[MonthlyUsage] = Field(min_length=12, max_length=12, description="未来 12 个月逐月峰谷用电量")
    timeout_seconds: float = Field(default=8.0, ge=1.0, le=30.0)

    @field_validator("list_url")
    @classmethod
    def _validate_scheme(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith(("http://", "https://", "file://")):
            raise ValueError("list_url 仅支持 http://、https:// 或 file:// 协议")
        return value

    @field_validator("monthly_usage")
    @classmethod
    def _validate_months(cls, value: list[MonthlyUsage]) -> list[MonthlyUsage]:
        months = sorted(item.month for item in value)
        if months != list(range(1, 13)):
            raise ValueError("monthly_usage 必须恰好包含 1-12 月各一条记录")
        return value


class TierChargeOut(BaseModel):
    tier_index: int
    lower_kwh: float
    upper_kwh: float | None
    rate_cny_per_kwh: float
    charged_kwh: float
    charge_cny: float


class MonthlyBillOut(BaseModel):
    month: int
    peak_kwh: float
    valley_kwh: float
    peak_charges: list[TierChargeOut]
    valley_charges: list[TierChargeOut]
    peak_energy_fee_cny: float
    valley_energy_fee_cny: float
    base_fee_cny: float
    monthly_total_cny: float


class RankedPlanOut(BaseModel):
    rank: int
    plan_id: str
    plan_name: str
    supplier: str | None
    regions: list[str]
    annual_total_cny: float
    detail_url: str
    list_url: str
    monthly_bills: list[MonthlyBillOut]


class ExcludedPlanOut(BaseModel):
    plan_id: str
    plan_name: str
    supplier: str | None
    regions: list[str]
    reason: str
    detail: str
    detail_url: str
    list_url: str


class CrawlFailureOut(BaseModel):
    url: str
    stage: str
    reason: str
    status: int | None


class CompareResponse(BaseModel):
    request_region: str
    list_url: str
    crawl: dict
    conclusion: dict
    ranked_plans: list[RankedPlanOut]
    excluded_plans: list[ExcludedPlanOut]
    failures: list[CrawlFailureOut]


# ---------- 电表校时整理 ----------


class CalibrationAnchorIn(BaseModel):
    seq: int = Field(ge=1, description="校时锚点的采样序号")
    trusted_time_utc: datetime = Field(description="该序号对应的可信 UTC 时间（ISO 8601，须带时区）")

    @field_validator("trusted_time_utc")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("trusted_time_utc 必须携带时区，如 2026-10-01T00:00:00Z")
        return value.astimezone(timezone.utc)


class CalibrateRequest(BaseModel):
    first_page_url: str = Field(min_length=1, description="供电商明细首页 URL，支持 http/https/file")
    anchors: list[CalibrationAnchorIn] = Field(min_length=2, description="校时锚点，至少两个，序号互不相同")
    timeout_seconds: float = Field(default=8.0, ge=1.0, le=30.0)

    @field_validator("first_page_url")
    @classmethod
    def _validate_scheme(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith(("http://", "https://", "file://")):
            raise ValueError("first_page_url 仅支持 http://、https:// 或 file:// 协议")
        return value

    @field_validator("anchors")
    @classmethod
    def _validate_anchor_seqs(cls, value: list[CalibrationAnchorIn]) -> list[CalibrationAnchorIn]:
        seqs = [anchor.seq for anchor in value]
        if len(set(seqs)) != len(seqs):
            raise ValueError("anchors 中采样序号必须互不相同")
        return value


class AnchorOut(BaseModel):
    seq: int
    trusted_time_utc: str
    device_time_utc: str | None
    offset_seconds: float | None
    status: str  # applied | missing | conflict | unparseable
    source_url: str | None


class TrustedRecordOut(BaseModel):
    seq: int
    device_time_utc: str
    corrected_time_utc: str
    offset_seconds: float
    energy_kwh: float
    source_url: str


class PendingRecordOut(BaseModel):
    seq: int
    device_time_utc: str | None
    device_time_raw: str
    energy_kwh: float | None
    source_url: str


class PendingReasonOut(BaseModel):
    code: str
    detail: str
    sources: list[str]


class PendingSegmentOut(BaseModel):
    start_seq: int
    end_seq: int
    reasons: list[PendingReasonOut]
    records: list[PendingRecordOut]


class CalibrateResponse(BaseModel):
    first_page_url: str
    verification_status: str  # verified | pending_verification
    note: str
    crawl: dict
    anchors: list[AnchorOut]
    trusted_records: list[TrustedRecordOut]
    pending_segments: list[PendingSegmentOut]
    failures: list[CrawlFailureOut]
