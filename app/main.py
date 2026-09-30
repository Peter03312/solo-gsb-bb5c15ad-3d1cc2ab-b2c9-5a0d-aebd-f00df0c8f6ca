"""FastAPI 入口。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .db import close_pool, open_pool
from .routers import calibrations, reports


@asynccontextmanager
async def lifespan(app: FastAPI):
    open_pool()  # 启动即建池并幂等初始化数据库
    try:
        yield
    finally:
        close_pool()


app = FastAPI(
    title="实验室检测报告放行 API",
    version="1.0.0",
    description=(
        "校准记录版本化管理与检测报告放行判定。\n\n"
        "- 校准区间左闭右开 [valid_from, valid_to)，同仪器当前版本区间不得重叠\n"
        "- 更正引用原记录并提交连续版本号，旧版保留可查，仅最新版参与判定\n"
        "- 报告仅在检测时间被“通过”校准覆盖时发布，并保存判定快照\n"
        "- 所有写事务运行在 SERIALIZABLE 隔离级别，冲突自动重试"
    ),
    lifespan=lifespan,
)

app.include_router(calibrations.router)
app.include_router(reports.router)


@app.get("/health", tags=["system"])
def health():
    from . import db

    assert db.pool is not None
    with db.pool.connection() as conn:
        version = conn.execute("SELECT version() AS v").fetchone()["v"]
    return {"status": "ok", "database": version}
