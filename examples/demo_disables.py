"""仪器禁用功能调用示例（标准库即可运行）：

    BASE_URL=http://localhost:8000 python3 examples/demo_disables.py

演示：
1. 登记校准 -> 正常放行
2. 登记禁用 -> 区间内报告被拒（返回禁用依据）；左闭右开端点
3. 撤销 -> 恢复放行；受影响历史报告查询随登记/撤销变化，原快照不改写
4. 并发：禁用登记 / 校准更正 / 报告提交同时进行 —— 判定可串行，
   绝不在已生效禁用区间内误放行
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

BASE = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
TAG = datetime.now(timezone.utc).strftime("%H%M%S")  # 避免与历史演示数据撞号


def call(method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def show(title: str, status: int, body: dict) -> None:
    print(f"\n== {title} -> HTTP {status} ==")
    print(json.dumps(body, indent=2, ensure_ascii=False))


def main() -> None:
    instrument = f"DEMO-{TAG}"

    status, body = call("POST", "/calibrations", {
        "instrument": instrument,
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to": "2027-01-01T00:00:00Z",
        "passed": True,
    })
    show("登记校准（全年通过）", status, body)
    cal_id = body["calibration"]["id"]

    status, body = call("POST", "/reports", {
        "instrument": instrument,
        "tested_at": "2026-03-15T08:00:00Z",
        "reference": f"{TAG}-R1",
    })
    show("禁用前：3 月报告正常放行", status, body)

    status, body = call("POST", "/disables", {
        "instrument": instrument,
        "disabled_from": "2026-04-01T00:00:00Z",
        "disabled_to": "2026-06-01T00:00:00Z",
        "reason": "进样泵故障",
    })
    show("登记禁用 [4/1, 6/1)", status, body)
    event_id = body["disable_event"]["id"]

    status, body = call("POST", "/reports", {
        "instrument": instrument,
        "tested_at": "2026-05-01T00:00:00Z",
        "reference": f"{TAG}-R2",
    })
    show("禁用区间内提交 -> 422 INSTRUMENT_DISABLED（含禁用依据）", status, body)

    status, body = call("POST", "/disables", {
        "instrument": instrument,
        "disabled_from": "2026-05-10T00:00:00Z",
        "disabled_to": "2026-07-01T00:00:00Z",
        "reason": "重叠登记",
    })
    show("重叠登记 -> 409", status, body)

    status, body = call("POST", f"/disables/{event_id}/revoke",
                        {"reason": "泵修复复核通过"})
    show("撤销禁用", status, body)
    status, body = call("POST", f"/disables/{event_id}/revoke")
    show("重复撤销 -> 409 ALREADY_REVOKED", status, body)

    status, body = call("POST", "/reports", {
        "instrument": instrument,
        "tested_at": "2026-05-01T00:00:00Z",
        "reference": f"{TAG}-R3",
    })
    show("撤销后同时点恢复放行", status, body)

    # 追溯登记：历史已发布报告立即出现在受影响列表，但报告与快照不改写
    call("POST", "/disables", {
        "instrument": instrument,
        "disabled_from": "2026-03-01T00:00:00Z",
        "disabled_to": "2026-04-01T00:00:00Z",
        "reason": "事后追溯：当时管路污染",
    })
    status, body = call("GET", f"/disables/affected-reports?instrument={instrument}")
    show("按当前禁用状态查询受影响的已发布报告（含禁用依据）", status, body)

    # ---- 并发三方：禁用登记 / 校准更正 / 报告提交 --------------------------
    race_instrument = f"RACE-{TAG}"
    _, cal = call("POST", "/calibrations", {
        "instrument": race_instrument,
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to": "2027-01-01T00:00:00Z",
        "passed": True,
    })
    race_cal_id = cal["calibration"]["id"]
    import time
    import threading

    start = threading.Barrier(8)

    def submit(i: int):
        start.wait()
        return call("POST", "/reports", {
            "instrument": race_instrument,
            "tested_at": "2026-06-01T00:00:00Z",
            "reference": f"{TAG}-RACE-{i}",
        })

    def register_disable():
        start.wait()
        return call("POST", "/disables", {
            "instrument": race_instrument,
            "disabled_from": "2026-05-01T00:00:00Z",
            "disabled_to": "2026-07-01T00:00:00Z",
            "reason": "并发故障",
        })

    def correct_calibration():
        start.wait()
        time.sleep(0.02)  # 让禁用与部分提交先飞一会
        return call("POST", "/calibrations/corrections", {
            "corrects_id": race_cal_id,
            "version": 2,
            "valid_from": "2026-01-01T00:00:00Z",
            "valid_to": "2027-01-01T00:00:00Z",
            "passed": True,
        })

    with ThreadPoolExecutor(max_workers=8) as pool:
        f_dis = pool.submit(register_disable)
        f_cal = pool.submit(correct_calibration)
        f_reps = [pool.submit(submit, i) for i in range(6)]
        dis_status, dis_body = f_dis.result()
        cal_status, cal_body = f_cal.result()
        rep_results = [f.result() for f in f_reps]

    print("\n== 并发结果（禁用登记 / 校准更正 / 6 份报告同时进行） ==")
    print("禁用登记:", dis_status, dis_body.get("disable_event", dis_body.get("detail")))
    print("校准更正:", cal_status, cal_body.get("calibration", cal_body.get("detail")))
    for i, (st, bd) in enumerate(rep_results):
        code = bd.get("reason_code") if st == 201 else bd["detail"]["code"]
        print(f"  报告#{i}: HTTP {st} {code}")

    # 尘埃落定：禁用已生效，区间内提交必须被阻断，校准是否更正都不影响
    status, body = call("POST", "/reports", {
        "instrument": race_instrument,
        "tested_at": "2026-06-01T00:00:00Z",
        "reference": f"{TAG}-AFTER",
    })
    show("禁用生效后提交 -> 必为 422 INSTRUMENT_DISABLED（绝不误放行）", status, body)
    assert status == 422 and body["detail"]["code"] == "INSTRUMENT_DISABLED"

    # 所有放行的并发报告必须创建于禁用生效之前（串行化保证）
    _, affected = call("GET", f"/disables/affected-reports?instrument={race_instrument}")
    print("\n禁用生效后受影响报告数:", affected["count"],
          "（这些是禁用前已放行的报告；其快照不改写，仅按当前状态列出）")


if __name__ == "__main__":
    main()
