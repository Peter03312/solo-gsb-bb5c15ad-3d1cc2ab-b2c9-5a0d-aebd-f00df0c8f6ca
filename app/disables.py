"""仪器禁用领域逻辑：禁用登记 / 撤销 / 命中判定 / 受影响已发布报告查询。

禁用区间 [disabled_from, disabled_to) 左闭右开，``disabled_to`` 为 NULL 表示
开放区间。仅“未撤销”（``revoked_at IS NULL``）事件参与判定；撤销后记录保留，
供追溯。所有写事务由调用方置于 SERIALIZABLE 隔离级别执行。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg.errors import ExclusionViolation

from .errors import blocked, conflict, not_found
from .models import serialize_disable_event

# 检测时间命中未撤销禁用区间：即使校准通过也拒绝放行
REASON_DISABLED = "INSTRUMENT_DISABLED"

# 命中未撤销禁用区间的查询（FOR UPDATE 时与禁用登记/撤销互斥）
_DISABLE_COVERAGE_SQL = """
    SELECT *
    FROM disable_events
    WHERE instrument = %s
      AND revoked_at IS NULL
      AND disabled_from <= %s
      AND (disabled_to IS NULL OR %s < disabled_to)
"""


def disable_brief(row: dict[str, Any]) -> dict[str, Any]:
    """放行判定中引用的禁用依据（精简结构）。"""
    return {
        "event_id": row["id"],
        "instrument": row["instrument"],
        "disabled_from": row["disabled_from"].isoformat(),
        "disabled_to": row["disabled_to"].isoformat() if row["disabled_to"] else None,
        "reason": row["reason"],
    }


def find_active_disable(conn, instrument: str, tested_at: datetime,
                        *, lock: bool) -> dict[str, Any] | None:
    """返回命中检测时间的未撤销禁用事件；无命中返回 None。

    ``lock=True`` 时对命中行加 FOR UPDATE 行锁；未命中时锁定该仪器全部
    未撤销事件 —— 与禁用登记（插入重叠区间）和撤销事务互斥，配合
    SERIALIZABLE 谓词锁，杜绝“禁用生效后误放行”。
    """
    with conn.cursor() as cur:
        cur.execute(
            _DISABLE_COVERAGE_SQL + (" FOR UPDATE" if lock else ""),
            (instrument, tested_at, tested_at),
        )
        hits = cur.fetchall()
        if hits:
            # 排他约束保证同仪器未撤销区间不重叠，至多一条
            return hits[0]
        if lock:
            cur.execute(
                "SELECT id FROM disable_events "
                "WHERE instrument = %s AND revoked_at IS NULL FOR UPDATE",
                (instrument,),
            )
            cur.fetchall()
    return None


def register_disable(conn, payload) -> dict[str, Any]:
    """登记禁用区间。无效区间/与未撤销区间重叠均拒绝。"""
    if (payload.disabled_to is not None
            and payload.disabled_to <= payload.disabled_from):
        raise blocked(
            "INVALID_INTERVAL",
            "禁用区间必须满足 disabled_from < disabled_to（左闭右开）",
        )

    with conn.cursor() as cur:
        # 先锁同仪器现有未撤销区间，与并发登记/发布在此串行
        cur.execute(
            "SELECT id FROM disable_events "
            "WHERE instrument = %s AND revoked_at IS NULL FOR UPDATE",
            (payload.instrument,),
        )
        cur.fetchall()

        # 应用层预检：给出可读错误；数据库排他约束为最终防线（含边界相接放行）
        cur.execute(
            """
            SELECT * FROM disable_events
            WHERE instrument = %s AND revoked_at IS NULL
              AND tstzrange(disabled_from, disabled_to, '[)')
                  && tstzrange(%s, %s, '[)')
            ORDER BY id
            """,
            (payload.instrument, payload.disabled_from, payload.disabled_to),
        )
        overlap = cur.fetchall()
        if overlap:
            raise conflict(
                "DISABLE_INTERVAL_OVERLAP",
                "与该仪器另一条未撤销禁用区间重叠（边界相接允许）",
                instrument=payload.instrument,
                conflicting_event_id=overlap[0]["id"],
            )
        try:
            cur.execute(
                """
                INSERT INTO disable_events
                    (instrument, disabled_from, disabled_to, reason)
                VALUES (%s, %s, %s, %s)
                RETURNING *
                """,
                (payload.instrument, payload.disabled_from,
                 payload.disabled_to, payload.reason),
            )
        except ExclusionViolation:
            raise conflict(
                "DISABLE_INTERVAL_OVERLAP",
                "与该仪器另一条未撤销禁用区间重叠（边界相接允许）",
                instrument=payload.instrument,
            )
        return cur.fetchone()


def revoke_disable(conn, event_id: int, note: str | None = None) -> dict[str, Any]:
    """按事件号撤销禁用。未知事件/已撤销事件明确拒绝；记录保留可追溯。"""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM disable_events WHERE id = %s FOR UPDATE",
                    (event_id,))
        row = cur.fetchone()
        if row is None:
            raise not_found("禁用事件", event_id)
        if row["revoked_at"] is not None:
            raise conflict(
                "ALREADY_REVOKED",
                f"禁用事件 {event_id} 已于 {row['revoked_at'].isoformat()} 撤销，"
                "不可重复撤销",
                event_id=event_id,
                revoked_at=row["revoked_at"].isoformat(),
            )
        cur.execute(
            """
            UPDATE disable_events
            SET revoked_at = now(), revoked_reason = %s
            WHERE id = %s AND revoked_at IS NULL
            RETURNING *
            """,
            (note, event_id),
        )
        return cur.fetchone()


def list_disable_events(conn, instrument: str | None, *,
                        include_revoked: bool) -> list[dict[str, Any]]:
    sql = "SELECT * FROM disable_events"
    where, params = [], []
    if instrument:
        where.append("instrument = %s")
        params.append(instrument)
    if not include_revoked:
        where.append("revoked_at IS NULL")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY instrument, id"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def get_disable_event(conn, event_id: int) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM disable_events WHERE id = %s", (event_id,))
        row = cur.fetchone()
    if row is None:
        raise not_found("禁用事件", event_id)
    return row


def affected_reports(conn, instrument: str | None = None) -> list[dict[str, Any]]:
    """按当前禁用状态查询受影响的已发布报告及对应禁用依据。

    实时计算：追溯登记/撤销后结果随之变化；已发布报告记录与其判定快照
    绝不因此改写。命中多条（历史上本不应发生，防御性去重）时取事件号最小者。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.*, d.id AS d_event_id
            FROM reports r
            JOIN disable_events d
              ON d.instrument = r.instrument
             AND d.revoked_at IS NULL
             AND d.disabled_from <= r.tested_at
             AND (d.disabled_to IS NULL OR r.tested_at < d.disabled_to)
            WHERE (%s::text IS NULL OR r.instrument = %s)
            ORDER BY r.instrument, r.tested_at, r.id
            """,
            (instrument, instrument),
        )
        rows = cur.fetchall()

    seen: set[int] = set()
    items: list[dict[str, Any]] = []
    event_cache: dict[int, dict[str, Any]] = {}
    with conn.cursor() as cur:
        for row in rows:
            report_id = row["id"]
            if report_id in seen:
                continue
            seen.add(report_id)
            event_id = row["d_event_id"]
            if event_id not in event_cache:
                cur.execute(
                    "SELECT * FROM disable_events WHERE id = %s", (event_id,)
                )
                event_cache[event_id] = cur.fetchone()
            items.append({
                "report_id": report_id,
                "reference": row["reference"],
                "instrument": row["instrument"],
                "tested_at": row["tested_at"].isoformat(),
                "snapshot": row["snapshot"],
                "disable_basis": disable_brief(event_cache[event_id]),
            })
    return items
