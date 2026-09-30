"""仪器禁用功能的正确性验证（直连数据库，不走 HTTP）。

覆盖：
1. 禁用登记：成功 / 无效区间拒绝 / 开放区间
2. 同仪器未撤销区间重叠拒绝（边界相接允许）；不同仪器互不影响
3. 撤销：成功 / 未知事件 404 / 重复撤销 409；撤销后重叠登记放行
4. 发布判定：命中未撤销禁用区间即使校准通过也 422，并返回禁用依据
5. 左闭右开端点：disabled_from 命中，disabled_to 不命中
6. 受影响已发布报告：登记后出现、撤销后消失；原放行记录与快照不改写
7. 并发：发布 vs 禁用登记 —— 禁用生效后不得误放行
8. 并发：同区间并发登记 —— 至多一个成功
9. 并发：发布 vs 撤销 —— 串行化，无脏放行/脏阻断矛盾
"""
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import HTTPException

import app.db as db
from app.models import CalibrationCreate, DisableCreate, ReportSubmit
from app.repository import create_calibration, publish_report
from app.transactions import run_tx
from app import disables as dom

failures = []
db.open_pool()
T = lambda y, m, d, hh=0: datetime(y, m, d, hh, tzinfo=timezone.utc)


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), "-", name, detail)
    if not cond:
        failures.append(name)


def reset():
    with db.pool.connection() as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    db.init_db()


def http_call(fn, *args):
    try:
        return 200, run_tx(lambda conn: fn(conn, *args))
    except HTTPException as e:
        return e.status_code, e.detail


def reg(instrument, t0, t1, reason="故障"):
    return http_call(dom.register_disable, DisableCreate(
        instrument=instrument, disabled_from=t0, disabled_to=t1, reason=reason))


def revoke(eid):
    return http_call(dom.revoke_disable, eid)


def publish(instrument, when, ref=None):
    return http_call(publish_report, ReportSubmit(
        instrument=instrument, tested_at=when, reference=ref))


def make_passed_cal(instrument="D1"):
    http_call(create_calibration, CalibrationCreate(
        instrument=instrument, valid_from=T(2026, 1, 1),
        valid_to=T(2027, 1, 1), passed=True))


# ---------- 1. 登记 / 无效区间 / 开放区间 ----------
reset()
s, ev = reg("D1", T(2026, 3, 1), T(2026, 4, 1), "泵故障")
check("禁用登记成功 200，事件号唯一", s == 200 and ev["id"] >= 1 and ev["revoked_at"] is None, str(s))
s, d = reg("D1", T(2026, 5, 1), T(2026, 5, 1))
check("起止相等 -> 422 INVALID_INTERVAL", s == 422 and d["code"] == "INVALID_INTERVAL", str(d))
s, d = reg("D1", T(2026, 5, 2), T(2026, 5, 1))
check("止早于起 -> 422 INVALID_INTERVAL", s == 422 and d["code"] == "INVALID_INTERVAL")
s, ev_open = reg("D1", T(2030, 1, 1), None, "长期停用")
check("开放禁用区间（止为 NULL）登记成功", s == 200 and ev_open["disabled_to"] is None)

# ---------- 2. 重叠 / 相接 / 跨仪器 ----------
s, touch = reg("D1", T(2026, 4, 1), T(2026, 5, 1))
check("边界相接 [3/1,4/1)+[4/1,5/1) 允许", s == 200, str(s))
s, d = reg("D1", T(2026, 4, 30), T(2026, 6, 1))
check("真重叠 -> 409 DISABLE_INTERVAL_OVERLAP",
      s == 409 and d["code"] == "DISABLE_INTERVAL_OVERLAP", str(d))
s, d = reg("D1", T(2025, 1, 1), T(2026, 3, 2))
check("起点落在既有区间内 -> 409", s == 409 and d["code"] == "DISABLE_INTERVAL_OVERLAP")
s, other = reg("D2", T(2026, 3, 1), T(2026, 4, 1))
check("不同仪器同时段区间互不影响", s == 200)
s, d = reg("D1", T(2031, 1, 1), None)
check("与开放区间重叠 -> 409", s == 409 and d["code"] == "DISABLE_INTERVAL_OVERLAP")

# ---------- 3. 撤销 ----------
s, d = http_call(dom.revoke_disable, 999999)
check("撤销未知事件 -> 404 NOT_FOUND", s == 404 and d["code"] == "NOT_FOUND", str(d))
s, rev = revoke(ev["id"])
check("撤销成功，revoked_at 置位、记录保留", s == 200 and rev["revoked_at"] is not None)
rows = run_tx(lambda c: dom.list_disable_events(c, "D1", include_revoked=False))
check("默认列表不含已撤销", all(r["id"] != ev["id"] for r in rows))
rows = run_tx(lambda c: dom.list_disable_events(c, "D1", include_revoked=True))
check("include_revoked 可追溯已撤销记录", any(r["id"] == ev["id"] for r in rows))
s, d = revoke(ev["id"])
check("重复撤销 -> 409 ALREADY_REVOKED", s == 409 and d["code"] == "ALREADY_REVOKED", str(d))
# 撤销后，与之重叠的新登记应放行（已撤销区间不参与排他约束）
s, ev2 = reg("D1", T(2026, 3, 15), T(2026, 3, 20), "新故障")
check("撤销后重叠区间可登记", s == 200, str(s))

# ---------- 4/5. 发布判定与端点 ----------
reset()
make_passed_cal("D3")
s, before = publish("D3", T(2026, 2, 1), "before")
check("禁用前：校准通过正常放行", s == 200 and before["released"] is True)
s, ev = reg("D3", T(2026, 6, 1), T(2026, 7, 1), "检测器故障")

s, d = publish("D3", T(2026, 6, 15), "inside")
check("区间内发布 -> 422 INSTRUMENT_DISABLED 且含禁用依据",
      s == 422 and d["code"] == "INSTRUMENT_DISABLED"
      and d["disable_basis"]["event_id"] == ev["id"]
      and d["disable_basis"]["reason"] == "检测器故障", str(d))
s, d = publish("D3", T(2026, 6, 1), "at-from")
check("恰为 disabled_from（左闭）-> 阻断", s == 422 and d["code"] == "INSTRUMENT_DISABLED")
s, at_to = publish("D3", T(2026, 7, 1), "at-to")
check("恰为 disabled_to（右开）-> 校准通过放行",
      s == 200 and at_to["released"] is True, str(s))

# 阻断不落库
with db.pool.connection() as c:
    n = c.execute("SELECT count(*) n FROM reports WHERE reference IN ('inside','at-from')").fetchone()["n"]
check("禁用阻断的报告不落库", n == 0)

# 撤销后恢复
s, _ = revoke(ev["id"])
s, after = publish("D3", T(2026, 6, 15), "after-revoke")
check("撤销后同时点可正常放行", s == 200 and after["released"] is True, str(s))

# 无校准仪器被禁用：禁用优先，仍返回禁用依据
s, ev_nc = reg("D-NO-CAL", T(2026, 1, 1), None)
s, d = publish("D-NO-CAL", T(2026, 5, 1))
check("无校准但被禁用 -> INSTRUMENT_DISABLED（禁用优先）",
      s == 422 and d["code"] == "INSTRUMENT_DISABLED", str(d))

# ---------- 6. 受影响报告 + 快照不改写 ----------
reset()
make_passed_cal("D4")
s, r1 = publish("D4", T(2026, 3, 1), "A1")
s, r2 = publish("D4", T(2026, 5, 1), "A2")
s, r3 = publish("D4", T(2026, 9, 1), "A3")
items = run_tx(lambda c: dom.affected_reports(c, "D4"), readonly=True)
check("无禁用时 affected-reports 为空", items == [])

s, ev = reg("D4", T(2026, 4, 1), T(2026, 8, 1), "追溯性故障登记")
items = run_tx(lambda c: dom.affected_reports(c, "D4"), readonly=True)
ids = {i["report_id"] for i in items}
check("登记后命中区间的已发布报告受影响（A2）",
      ids == {r2["report_id"]}, str(ids))
check("受影响项含对应禁用依据",
      items[0]["disable_basis"]["event_id"] == ev["id"]
      and items[0]["snapshot"]["released"] is True
      and items[0]["snapshot"]["reason_code"] == "PASSED_CALIBRATION_COVERAGE")
# 原放行记录与快照不改写
with db.pool.connection() as c:
    row = c.execute("SELECT released, snapshot FROM reports WHERE id=%s",
                    (r2["report_id"],)).fetchone()
check("原报告仍为 released=true 且快照不改写",
      row["released"] is True
      and row["snapshot"]["reason_code"] == "PASSED_CALIBRATION_COVERAGE")

s, _ = revoke(ev["id"])
items = run_tx(lambda c: dom.affected_reports(c, "D4"), readonly=True)
check("撤销后 affected-reports 随之清空", items == [])

# 开放区间禁用影响全部已发布报告
s, ev_open = reg("D4", T(2020, 1, 1), None)
items = run_tx(lambda c: dom.affected_reports(c, "D4"), readonly=True)
check("开放禁用区间影响该仪器全部已发布报告",
      {i["report_id"] for i in items} ==
      {r1["report_id"], r2["report_id"], r3["report_id"]})
items_all = run_tx(lambda c: dom.affected_reports(c, None), readonly=True)
check("不带 instrument 时跨仪器查询受影响报告",
      any(i["report_id"] in {r1["report_id"], r2["report_id"], r3["report_id"]}
          for i in items_all))

# ---------- 7. 并发：发布 vs 禁用登记 ----------
reset()
make_passed_cal("D5")
N = 16
barrier = threading.Barrier(N + 1)


def race_publish(i):
    barrier.wait()
    try:
        r = run_tx(lambda conn: publish_report(conn, ReportSubmit(
            instrument="D5", tested_at=T(2026, 6, 1),
            reference=f"race-{i}")))
        return ("released", r["report_id"])
    except HTTPException as e:
        return (e.detail["code"], None)


def race_register():
    barrier.wait()
    try:
        row = run_tx(lambda conn: dom.register_disable(conn, DisableCreate(
            instrument="D5", disabled_from=T(2026, 6, 1),
            disabled_to=T(2026, 6, 2), reason="并发登记的禁用")))
        return ("registered", row["id"])
    except HTTPException as e:
        return (e.detail["code"], None)


with ThreadPoolExecutor(N + 1) as ex:
    pf = [ex.submit(race_publish, i) for i in range(N)]
    rf = ex.submit(race_register)
    presults = [f.result() for f in pf]
    rresult = rf.result()

check("并发禁用登记最终生效", rresult[0] == "registered", str(rresult))
bad_codes = {c for c, _ in presults if c not in ("released", "INSTRUMENT_DISABLED")}
check("发布结果仅可能为放行或禁用阻断（无 TRANSACTION_CONFLICT 外泄）",
      not bad_codes, str(bad_codes))
with db.pool.connection() as c:
    ev_row = c.execute(
        "SELECT id, created_at FROM disable_events WHERE instrument='D5'").fetchone()
    late = c.execute("""
        SELECT r.id FROM reports r
        WHERE r.instrument='D5' AND r.reference LIKE 'race-%%'
          AND r.created_at > %s
    """, (ev_row["created_at"],)).fetchall()
check("禁用生效后无误放行（提交晚于禁用生效的报告不存在）", not late,
      f"{len(late)} late releases")
inside_now = publish("D5", T(2026, 6, 1, 12))
check("尘埃落定后区间内发布必被禁用阻断",
      inside_now[0] == 422 and inside_now[1]["code"] == "INSTRUMENT_DISABLED",
      str(inside_now))

# ---------- 8. 并发：同区间并发登记至多一个成功 ----------
reset()
barrier = threading.Barrier(8)


def race_reg(i):
    barrier.wait()
    try:
        row = run_tx(lambda conn: dom.register_disable(conn, DisableCreate(
            instrument="D6", disabled_from=T(2026, 1, 1),
            disabled_to=T(2026, 12, 1), reason=f"原因{i}")))
        return ("ok", row["id"])
    except HTTPException as e:
        return (e.detail["code"], None)


with ThreadPoolExecutor(8) as ex:
    results = list(ex.map(race_reg, range(8)))
ok = [r for r in results if r[0] == "ok"]
ov = [r for r in results if r[0] == "DISABLE_INTERVAL_OVERLAP"]
other = [r for r in results if r[0] not in ("ok", "DISABLE_INTERVAL_OVERLAP")]
check("8 个重叠并发登记：恰 1 个成功", len(ok) == 1,
      f"ok={len(ok)} overlap={len(ov)} other={other}")
check("其余全部重叠冲突", len(ov) == 7 and not other, str(other))
with db.pool.connection() as c:
    n = c.execute(
        "SELECT count(*) n FROM disable_events WHERE instrument='D6'").fetchone()["n"]
check("失败事务未留脏行（仅 1 条）", n == 1)

# ---------- 9. 并发：发布 vs 撤销 ----------
reset()
make_passed_cal("D7")
s, ev = reg("D7", T(2026, 6, 1), T(2026, 6, 2))
N = 12
barrier = threading.Barrier(N + 1)


def race_publish2(i):
    barrier.wait()
    try:
        run_tx(lambda conn: publish_report(conn, ReportSubmit(
            instrument="D7", tested_at=T(2026, 6, 1, 1),
            reference=f"rv-{i}")))
        return "released"
    except HTTPException as e:
        return e.detail["code"]


def race_revoke():
    barrier.wait()
    try:
        run_tx(lambda conn: dom.revoke_disable(conn, ev["id"]))
        return "revoked"
    except HTTPException as e:
        return e.detail["code"]


with ThreadPoolExecutor(N + 1) as ex:
    pf = [ex.submit(race_publish2, i) for i in range(N)]
    vf = ex.submit(race_revoke)
    pres = [f.result() for f in pf]
    vres = vf.result()

check("并发撤销最终生效", vres == "revoked", str(vres))
weird = {c for c in pres if c not in ("released", "INSTRUMENT_DISABLED")}
check("发布结果仅放行/禁用阻断两种", not weird, str(weird))
with db.pool.connection() as c:
    n_released = c.execute(
        "SELECT count(*) n FROM reports WHERE reference LIKE 'rv-%'").fetchone()["n"]
check(f"放行数 0..{N} 之间（取决于串行序），其余被阻断",
      0 <= n_released <= N and n_released + pres.count("INSTRUMENT_DISABLED") == N,
      f"released={n_released}")

print()
if failures:
    print(f"{len(failures)} 项失败:", failures)
    sys.exit(1)
print("全部禁用功能验证通过")
