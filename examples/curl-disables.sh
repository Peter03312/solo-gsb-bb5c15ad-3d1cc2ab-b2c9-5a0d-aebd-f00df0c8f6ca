#!/usr/bin/env bash
# 仪器禁用端到端调用示例：
#   放行 -> 登记禁用 -> 禁用区间内拒绝放行（返回禁用依据）-> 端点边界
#   -> 重叠/无效登记被拒 -> 撤销 -> 恢复放行 -> 受影响报告查询（追溯登记/撤销随动）
# 依赖：curl（jq 可选，没有时用 python -m json.tool）
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { if command -v jq >/dev/null 2>&1; then jq .; else python3 -m json.tool; fi; }
post() { curl -sS -o /tmp/dis_body.json -w '%{http_code}' "$@"; }

echo "== 1. 登记校准（GC-09，2026 全年通过） =="
curl -sS -X POST "$BASE/calibrations" -H 'Content-Type: application/json' -d '{
  "instrument": "GC-09",
  "valid_from": "2026-01-01T00:00:00Z",
  "valid_to":   "2027-01-01T00:00:00Z",
  "passed": true
}' | pp

echo "== 2. 禁用前：3 月的报告正常放行 =="
curl -sS -X POST "$BASE/reports" -H 'Content-Type: application/json' -d '{
  "instrument": "GC-09",
  "tested_at": "2026-03-10T08:00:00Z",
  "reference": "DIS-REP-1"
}' | pp

echo "== 3. 登记禁用区间 [2026-04-01, 2026-06-01)，原因为进样泵故障 =="
EV=$(curl -sS -X POST "$BASE/disables" -H 'Content-Type: application/json' -d '{
  "instrument": "GC-09",
  "disabled_from": "2026-04-01T00:00:00Z",
  "disabled_to":   "2026-06-01T00:00:00Z",
  "reason": "进样泵故障"
}')
echo "$EV" | pp
EVENT_ID=$(echo "$EV" | python3 -c 'import sys,json;print(json.load(sys.stdin)["disable_event"]["id"])')

echo "== 4. 禁用区间内提交报告 -> 422 INSTRUMENT_DISABLED（即使校准通过），detail 含禁用依据 =="
CODE=$(post -X POST "$BASE/reports" -H 'Content-Type: application/json' -d '{
  "instrument": "GC-09",
  "tested_at": "2026-05-01T00:00:00Z",
  "reference": "DIS-REP-BLOCKED"
}')
echo "HTTP $CODE"
cat /tmp/dis_body.json | pp

echo "== 5. 左闭右开端点：恰为 disabled_from 阻断；恰为 disabled_to 放行 =="
CODE=$(post -X POST "$BASE/reports" -H 'Content-Type: application/json' -d '{"instrument":"GC-09","tested_at":"2026-04-01T00:00:00Z","reference":"DIS-AT-FROM"}')
echo "at disabled_from -> HTTP $CODE / $(cat /tmp/dis_body.json | python3 -c 'import sys,json;print(json.load(sys.stdin)["detail"]["code"])')"
curl -sS -X POST "$BASE/reports" -H 'Content-Type: application/json' -d '{"instrument":"GC-09","tested_at":"2026-06-01T00:00:00Z","reference":"DIS-AT-TO"}' | pp

echo "== 6. 重叠登记 -> 409 DISABLE_INTERVAL_OVERLAP（响应含 conflicting_event_id） =="
CODE=$(post -X POST "$BASE/disables" -H 'Content-Type: application/json' -d '{
  "instrument": "GC-09",
  "disabled_from": "2026-05-15T00:00:00Z",
  "disabled_to":   "2026-07-01T00:00:00Z",
  "reason": "重叠的禁用"
}')
echo "HTTP $CODE / $(cat /tmp/dis_body.json | python3 -c 'import sys,json;print(json.load(sys.stdin)["detail"]["code"])')"

echo "== 7. 边界相接（[..,6/1)+[6/1,7/1)）允许 -> 201 =="
curl -sS -X POST "$BASE/disables" -H 'Content-Type: application/json' -d '{
  "instrument": "GC-09",
  "disabled_from": "2026-06-01T00:00:00Z",
  "disabled_to":   "2026-07-01T00:00:00Z",
  "reason": "泵更换后复测期"
}' | pp

echo "== 8. 无效区间（止<=起）-> 422 INVALID_INTERVAL =="
CODE=$(post -X POST "$BASE/disables" -H 'Content-Type: application/json' -d '{
  "instrument": "GC-09",
  "disabled_from": "2026-08-01T00:00:00Z",
  "disabled_to":   "2026-08-01T00:00:00Z",
  "reason": "坏区间"
}')
echo "HTTP $CODE / $(cat /tmp/dis_body.json | python3 -c 'import sys,json;print(json.load(sys.stdin)["detail"]["code"])')"

echo "== 9. 撤销禁用（事件号 $EVENT_ID，可带撤销备注；不带 body 亦可） =="
curl -sS -X POST "$BASE/disables/$EVENT_ID/revoke" -H 'Content-Type: application/json' \
  -d '{"reason": "泵修复并复核通过"}' | pp

echo "== 10. 撤销未知事件 -> 404；重复撤销 -> 409 ALREADY_REVOKED =="
CODE=$(post -X POST "$BASE/disables/999999/revoke")
echo "unknown -> HTTP $CODE / $(cat /tmp/dis_body.json | python3 -c 'import sys,json;print(json.load(sys.stdin)["detail"]["code"])')"
CODE=$(post -X POST "$BASE/disables/$EVENT_ID/revoke")
echo "repeat  -> HTTP $CODE / $(cat /tmp/dis_body.json | python3 -c 'import sys,json;print(json.load(sys.stdin)["detail"]["code"])')"

echo "== 11. 撤销后原区间内恢复校准判定（5 月时点重新放行） =="
curl -sS -X POST "$BASE/reports" -H 'Content-Type: application/json' -d '{
  "instrument": "GC-09",
  "tested_at": "2026-05-10T00:00:00Z",
  "reference": "DIS-REP-AFTER-REVOKE"
}' | pp

echo "== 12. 追溯登记一个覆盖 3 月的禁用区间，受影响的历史已发布报告立即可查 =="
curl -sS -X POST "$BASE/disables" -H 'Content-Type: application/json' -d '{
  "instrument": "GC-09",
  "disabled_from": "2026-03-01T00:00:00Z",
  "disabled_to":   "2026-04-01T00:00:00Z",
  "reason": "事后追溯：当时管路污染"
}' > /dev/null
curl -sS "$BASE/disables/affected-reports?instrument=GC-09" | pp

echo "== 13. 事件留痕查询：列表（默认仅未撤销）/ 全量 / 单事件 =="
curl -sS "$BASE/disables?instrument=GC-09" \
  | python3 -c 'import sys,json;print("active:", [(i["id"],i["reason"]) for i in json.load(sys.stdin)["items"]])'
curl -sS "$BASE/disables?instrument=GC-09&include_revoked=true" \
  | python3 -c 'import sys,json;print("all:   ", [(i["id"],i["revoked"]) for i in json.load(sys.stdin)["items"]])'
curl -sS "$BASE/disables/$EVENT_ID" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin)["disable_event"];print("event",d["id"],"revoked_reason=",d["revoked_reason"])'
