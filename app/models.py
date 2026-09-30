"""Pydantic 请求/响应模型。时间统一按带时区时间戳处理。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator


def as_utc(value: datetime) -> datetime:
    """无时区时间戳按 UTC 处理；有时区的统一转换到 UTC。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class CalibrationCreate(BaseModel):
    instrument: str = Field(..., min_length=1, description="仪器标识")
    valid_from: datetime
    valid_to: datetime | None = Field(None, description="为空表示开放区间（持续有效）")
    passed: bool

    _normalize_from = field_validator("valid_from")(lambda cls, v: as_utc(v))
    _normalize_to = field_validator("valid_to")(lambda cls, v: as_utc(v) if v else v)


class CalibrationCorrect(BaseModel):
    corrects_id: int = Field(..., description="被更正的任意历史版本 ID（通常传原记录/最新版 ID）")
    version: int = Field(..., ge=1, description="必须为该校准链当前最新版本号 + 1（连续版本号）")
    valid_from: datetime
    valid_to: datetime | None = None
    passed: bool

    _normalize_from = field_validator("valid_from")(lambda cls, v: as_utc(v))
    _normalize_to = field_validator("valid_to")(lambda cls, v: as_utc(v) if v else v)


class ReportSubmit(BaseModel):
    instrument: str = Field(..., min_length=1)
    tested_at: datetime = Field(..., description="检测时间，据此判断校准是否覆盖")
    reference: str | None = Field(None, description="可选业务报告编号；提供则需全局唯一")

    _normalize_tested = field_validator("tested_at")(lambda cls, v: as_utc(v))


class DisableCreate(BaseModel):
    instrument: str = Field(..., min_length=1, description="被禁用的仪器标识")
    disabled_from: datetime = Field(..., description="禁用起（含），左闭右开")
    disabled_to: datetime | None = Field(None, description="禁用止（不含）；为空表示持续禁用")
    reason: str = Field(..., min_length=1, description="禁用原因")

    _normalize_from = field_validator("disabled_from")(lambda cls, v: as_utc(v))
    _normalize_to = field_validator("disabled_to")(lambda cls, v: as_utc(v) if v else v)

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("禁用原因不能为空")
        return v


class DisableRevoke(BaseModel):
    # 撤销备注（可选）；请求体整体可省略，POST /disables/{id}/revoke 直接可用
    reason: str | None = Field(None, description="撤销备注，随记录保留供追溯")

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("撤销备注不能为空或纯空白")
        return v


def serialize_disable_event(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "instrument": row["instrument"],
        "disabled_from": row["disabled_from"].isoformat(),
        "disabled_to": row["disabled_to"].isoformat() if row["disabled_to"] else None,
        "reason": row["reason"],
        "revoked": row["revoked_at"] is not None,
        "revoked_at": row["revoked_at"].isoformat() if row["revoked_at"] else None,
        "revoked_reason": row.get("revoked_reason"),
        "created_at": row["created_at"].isoformat(),
    }


def serialize_calibration(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "chain_id": row["chain_id"],
        "version": row["version"],
        "instrument": row["instrument"],
        "valid_from": row["valid_from"].isoformat(),
        "valid_to": row["valid_to"].isoformat() if row["valid_to"] else None,
        "passed": row["passed"],
        "is_current": row["is_current"],
        "corrects_id": row["corrects_id"],
        "created_at": row["created_at"].isoformat(),
    }
