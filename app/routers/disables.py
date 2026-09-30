"""仪器禁用接口。"""
from __future__ import annotations

from fastapi import APIRouter, Query

from .. import disables as domain
from ..models import DisableCreate, DisableRevoke, serialize_disable_event
from ..transactions import run_tx

router = APIRouter(prefix="/disables", tags=["disables"])


@router.post("", status_code=201)
def register(payload: DisableCreate):
    """登记仪器禁用区间（左闭右开，止点可为空）。

    - 无效区间（disabled_to <= disabled_from）-> 422 INVALID_INTERVAL
    - 与同仪器未撤销区间重叠 -> 409 DISABLE_INTERVAL_OVERLAP（边界相接允许）
    """
    row = run_tx(lambda conn: domain.register_disable(conn, payload))
    return {"disable_event": serialize_disable_event(row)}


@router.post("/{event_id}/revoke", status_code=200)
def revoke(event_id: int, payload: DisableRevoke | None = None):
    """按唯一事件号撤销禁用；记录保留供追溯。

    请求体可省略（直接 POST 无 body），亦可提供 ``{"reason": "修复完成"}``
    作为撤销备注。

    - 未知事件 -> 404 NOT_FOUND
    - 已撤销事件 -> 409 ALREADY_REVOKED
    """
    note = payload.reason if payload is not None else None
    row = run_tx(lambda conn: domain.revoke_disable(conn, event_id, note))
    return {"disable_event": serialize_disable_event(row)}


# 注意：静态路由需在 /{event_id} 之前声明
@router.get("/affected-reports")
def affected_reports(
    instrument: str | None = Query(None, description="可选，按仪器过滤"),
):
    """按当前禁用状态查询受影响的已发布报告及对应禁用依据。

    实时计算：追溯登记或撤销后结果随之变化；已发布报告记录与快照不改写。
    """
    items = run_tx(
        lambda conn: domain.affected_reports(conn, instrument), readonly=True)
    return {"items": items, "count": len(items)}


@router.get("")
def list_disables(
    instrument: str | None = Query(None),
    include_revoked: bool = Query(False, description="是否包含已撤销事件（追溯用）"),
):
    """列出禁用事件；默认仅未撤销，加 include_revoked=true 可追溯全部。"""
    rows = run_tx(
        lambda conn: domain.list_disable_events(
            conn, instrument, include_revoked=include_revoked),
        readonly=True,
    )
    return {"items": [serialize_disable_event(r) for r in rows]}


@router.get("/{event_id}")
def get_disable(event_id: int):
    """按事件号查询单条禁用事件（含已撤销，供追溯）。"""
    row = run_tx(lambda conn: domain.get_disable_event(conn, event_id),
                 readonly=True)
    return {"disable_event": serialize_disable_event(row)}
