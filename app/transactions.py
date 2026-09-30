"""事务封装：SERIALIZABLE 隔离 + 串行化失败/死锁自动重试。"""
from __future__ import annotations

import time
from typing import TypeVar

from psycopg import OperationalError
from psycopg import IsolationLevel
from psycopg.errors import DeadlockDetected, SerializationFailure
from psycopg_pool import PoolTimeout

from . import db
from .config import get_settings

T = TypeVar("T")

# 40001 = serialization_failure；40P01 = deadlock_detected
_RETRYABLE = (SerializationFailure, DeadlockDetected)


def run_tx(fn, *, readonly: bool = False, retries: int | None = None) -> T:
    """在 SERIALIZABLE 事务中执行 ``fn(conn)``；冲突时整体重试。

    ``fn`` 内部抛出的 HTTPException 会原样透传（不重试）。
    数据库层约束冲突同样透传，由各路由映射为 409。
    """
    settings = get_settings()
    attempts = settings.tx_max_retries if retries is None else retries
    assert db.pool is not None

    last_error: Exception | None = None
    for attempt in range(attempts + 1):
        try:
            conn = db.pool.getconn()
        except PoolTimeout:
            from fastapi import HTTPException

            raise HTTPException(status_code=503, detail={
                "code": "SERVICE_BUSY",
                "message": "数据库连接池繁忙，请稍后重试",
            })
        try:
            conn.read_only = readonly
            conn.isolation_level = IsolationLevel.SERIALIZABLE
            try:
                with conn.transaction():
                    result = fn(conn)
                return result
            finally:
                conn.read_only = False
                conn.isolation_level = None
        except _RETRYABLE as exc:
            last_error = exc
            if attempt >= attempts:
                break
            time.sleep(settings.tx_retry_backoff * (2**attempt))
        except OperationalError as exc:
            # 连接级故障也做一次有限重试
            last_error = exc
            if attempt >= attempts:
                break
            time.sleep(settings.tx_retry_backoff * (2**attempt))
        finally:
            db.pool.putconn(conn)

    from fastapi import HTTPException

    raise HTTPException(status_code=409, detail={
        "code": "TRANSACTION_CONFLICT",
        "message": "并发事务冲突，重试后仍失败，请稍后重试",
        "database_error": str(last_error),
    })
