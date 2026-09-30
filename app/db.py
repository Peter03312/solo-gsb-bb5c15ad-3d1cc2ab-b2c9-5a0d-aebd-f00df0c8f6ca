"""PostgreSQL 连接池与数据库初始化（幂等）。"""
from __future__ import annotations

import time

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings

pool: ConnectionPool | None = None

DDL = """
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- 校准版本链：一条链对应同一台仪器同一次校准的全部版本（v1 + 历次更正）
CREATE TABLE IF NOT EXISTS calibration_chains (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    instrument   TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS calibration_versions (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chain_id     BIGINT      NOT NULL REFERENCES calibration_chains(id),
    version      INTEGER     NOT NULL CHECK (version >= 1),
    instrument   TEXT        NOT NULL,
    valid_from   TIMESTAMPTZ NOT NULL,
    valid_to     TIMESTAMPTZ,                       -- NULL = 开放区间（持续有效）
    passed       BOOLEAN     NOT NULL,
    corrects_id  BIGINT REFERENCES calibration_versions(id),  -- v1 为 NULL
    is_current   BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_calibration_interval CHECK (valid_to IS NULL OR valid_to > valid_from)
);

-- 同一条链同一版本号只能有一条；更正必须使用连续版本号由应用层保证
CREATE UNIQUE INDEX IF NOT EXISTS uq_calibration_chain_version
    ON calibration_versions (chain_id, version);

-- 每条链至多一个最新版 —— 并发更正也不可能产生“双最新版”
CREATE UNIQUE INDEX IF NOT EXISTS uq_calibration_one_current
    ON calibration_versions (chain_id) WHERE is_current;

-- 同仪器所有“最新版”的有效区间 [valid_from, valid_to) 不得重叠；
-- 旧版本 is_current = FALSE，不参与判定与约束。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'excl_current_instrument_interval'
    ) THEN
        ALTER TABLE calibration_versions
            ADD CONSTRAINT excl_current_instrument_interval
            EXCLUDE USING gist (
                instrument WITH =,
                tstzrange(valid_from, valid_to, '[)') WITH &&
            ) WHERE (is_current);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_calibration_instrument_current
    ON calibration_versions (instrument) WHERE is_current;
CREATE INDEX IF NOT EXISTS ix_calibration_chain ON calibration_versions (chain_id);

-- 已发布报告（阻断的报告不落库），快照发布当时的判定依据
CREATE TABLE IF NOT EXISTS reports (
    id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    reference            TEXT,                          -- 业务侧报告编号（可选，唯一）
    instrument           TEXT        NOT NULL,
    tested_at            TIMESTAMPTZ NOT NULL,
    released             BOOLEAN     NOT NULL,
    basis_calibration_id BIGINT REFERENCES calibration_versions(id),
    snapshot             JSONB       NOT NULL,          -- 发布时判定快照（不可变）
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_report_reference
    ON reports (reference) WHERE reference IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_report_instrument_tested
    ON reports (instrument, tested_at);

-- 仪器禁用事件：校准合格后仍可因故障禁用仪器。
-- 区间左闭右开 [disabled_from, disabled_to)；disabled_to 为 NULL 表示开放区间。
-- revoked_at 非空即“已撤销”，记录保留仅供追溯，不再参与判定。
CREATE TABLE IF NOT EXISTS disable_events (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,  -- 唯一事件号
    instrument    TEXT        NOT NULL,
    disabled_from TIMESTAMPTZ NOT NULL,
    disabled_to   TIMESTAMPTZ,                       -- NULL = 开放区间（持续禁用）
    reason        TEXT        NOT NULL,
    revoked_at    TIMESTAMPTZ,
    revoked_reason TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_disable_instrument_active
    ON disable_events (instrument) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_disable_instrument_all
    ON disable_events (instrument, created_at);

-- 同仪器“未撤销”的禁用区间不得重叠；边界相接（[..,a) + [a,..)）允许。
-- 已撤销（revoked_at 非空）记录不参与约束，永久保留可追溯。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'excl_active_disable_interval'
    ) THEN
        ALTER TABLE disable_events
            ADD CONSTRAINT excl_active_disable_interval
            EXCLUDE USING gist (
                instrument WITH =,
                tstzrange(disabled_from, disabled_to, '[)') WITH &&
            ) WHERE (revoked_at IS NULL);
    END IF;
END $$;
"""


def open_pool() -> ConnectionPool:
    global pool
    settings = get_settings()
    # 容器编排中 db 可能尚未就绪：带退避地等待
    deadline = time.monotonic() + 60
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            candidate = ConnectionPool(
                settings.database_url,
                min_size=settings.db_pool_min_size,
                max_size=settings.db_pool_max_size,
                timeout=settings.db_acquire_timeout,
                kwargs={"row_factory": dict_row, "connect_timeout": settings.db_connect_timeout},
                open=True,
            )
            candidate.wait(timeout=10)
            pool = candidate
            init_db()
            return pool
        except Exception as exc:  # pragma: no cover - 启动期容错
            last_error = exc
            time.sleep(1.0)
    raise RuntimeError(f"无法连接数据库: {last_error}")


def close_pool() -> None:
    global pool
    if pool is not None:
        pool.close()
        pool = None


def init_db() -> None:
    """幂等建表/建约束。应用启动时自动执行，无需手工初始化。"""
    assert pool is not None
    with pool.connection() as conn:
        conn.execute(DDL)
