# 实验室检测报告放行服务

基于 **Python + FastAPI + PostgreSQL** 的 HTTP API：管理仪器校准记录（含有效期、通过/未通过结论、更正版本链）
与**仪器故障禁用区间**，并据此对检测报告做放行判定。

## 核心规则

- 校准区间为 **左闭右开** `[valid_from, valid_to)`；`valid_to` 为 `null` 表示开放区间（持续有效）。
- 同一仪器**当前版本**的有效区间**不得重叠**，由 PostgreSQL GiST `EXCLUDE` 约束在数据库层强制。
- 更正必须**引用原记录**（任意历史版本 ID）并提交**连续版本号**（当前最新版 + 1）。
- 旧版本全部保留可查，`is_current=true` 的唯一最新版才参与判定。
- 版本冲突、区间冲突一律拒绝（`409`），错误体含机器可读原因码。
- 仪器校准合格后仍可能因故障被禁用：禁用以**唯一事件号**登记，含仪器、起止时间与原因；
  区间同样**左闭右开** `[disabled_from, disabled_to)`，止点可为空（持续禁用）。
  同仪器**未撤销**禁用区间不得重叠（边界相接允许），由 GiST `EXCLUDE` 约束强制。
- 禁用可按事件号**撤销**；撤销只置 `revoked_at`，记录永久保留供追溯，不可重复撤销。
  未知事件 `404`、已撤销事件 `409`、无效区间 `422`、重叠登记 `409`，一律明确拒绝。
- 报告放行条件：检测时间**未命中任何未撤销禁用区间**，且被该仪器一条**结论为通过**的**当前**
  校准版本覆盖；命中禁用时即使当前校准通过也拒绝放行（`422 INSTRUMENT_DISABLED`）并返回禁用依据。
  成功时返回所据校准版本，并保存发布时判定快照。
- 校准缺失 / 时间在区间外 / 校准未通过：拒绝放行（`422`），返回阻断原因码与说明。
- 已发布报告的快照**永不改写**；更正校准或追溯登记/撤销禁用后可按当前状态重算/查询，
  列出受影响报告，但原放行记录与快照保持原值。
- 更正、禁用变更与发布事务均运行在 **SERIALIZABLE** 隔离级别并对冲突自动重试，
  配合唯一部分索引、行锁与排他约束，杜绝“双最新版”“禁用区间内误放行”和“依据过期校准放行”。

## 一键启动

需要 Docker 与 Docker Compose（v2）。

```bash
docker compose up --build
```

启动后：

- API 服务地址与端口：**http://localhost:8000**
- 交互式 API 文档（Swagger UI）：**http://localhost:8000/docs**
- OpenAPI JSON：http://localhost:8000/openapi.json
- 健康检查：http://localhost:8000/health
- PostgreSQL：`localhost:5432`（库/用户/口令均为 `lab`）

数据库（扩展 `btree_gist`、全部表、唯一/排除约束与索引）在 API 首次启动时**幂等自动初始化**，
无需手工执行 SQL；`db` 容器带健康检查，就绪后 API 才启动。

停止并清理：

```bash
docker compose down          # 保留数据卷
docker compose down -v       # 同时删除数据库数据卷
```

## 配置

通过环境变量配置（见 `.env.example`）：

| 变量 | 默认值（容器内） | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://lab:lab@db:5432/lab` | PostgreSQL 连接串 |
| `DB_POOL_MIN_SIZE` | `1` | 连接池最小连接数 |
| `DB_POOL_MAX_SIZE` | `10` | 连接池最大连接数 |
| `DB_CONNECT_TIMEOUT` | `10` | 建连超时（秒） |
| `DB_ACQUIRE_TIMEOUT` | `10` | 从连接池获取连接的超时（秒），超时返回 `503 SERVICE_BUSY` |
| `TX_MAX_RETRIES` | `5` | 串行化/死锁冲突时事务最大重试次数 |
| `TX_RETRY_BACKOFF` | `0.05` | 重试退避基数（秒，指数退避） |

本地不用容器运行时：准备好 PostgreSQL，复制 `.env.example` 为 `.env` 后执行

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## API 一览

### 校准

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/calibrations` | 登记原始校准（版本链 v1） |
| POST | `/calibrations/corrections` | 提交更正版本（引用原记录 + 连续版本号） |
| GET | `/calibrations?instrument=&current_only=` | 列表（旧版保留可查） |
| GET | `/calibrations/{id}` | 单条校准（任意版本） |
| GET | `/calibrations/drift/recompute?instrument=` | 按当前有效性重算，返回与快照结论不同的报告 |

### 报告

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/reports` | 提交报告并判定放行（禁用判定优先于校准） |
| GET | `/reports?instrument=` | 已发布报告列表（含不可变快照） |
| GET | `/reports/{id}` | 单条报告 |
| GET | `/reports/drift/recompute` | 同上，重算漂移 |

### 仪器禁用（本轮新增）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/disables` | 登记禁用区间（事件号自动生成；左闭右开，止点可空） |
| POST | `/disables/{event_id}/revoke` | 按事件号撤销禁用（请求体可省略；可带 `{"reason": "..."}` 备注） |
| GET | `/disables?instrument=&include_revoked=` | 禁用事件列表（默认仅未撤销；`include_revoked=true` 查全部留痕） |
| GET | `/disables/{event_id}` | 按事件号查单条（含已撤销，供追溯） |
| GET | `/disables/affected-reports?instrument=` | 按**当前**禁用状态查询受影响的已发布报告及禁用依据 |

## 调用示例

### 1. 登记校准

```bash
curl -sS -X POST http://localhost:8000/calibrations \
  -H 'Content-Type: application/json' \
  -d '{
        "instrument": "HPLC-01",
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to":   "2026-07-01T00:00:00Z",
        "passed": true
      }'
```

返回：

```json
{
  "calibration": {
    "id": 1, "chain_id": 1, "version": 1, "instrument": "HPLC-01",
    "valid_from": "2026-01-01T00:00:00+00:00",
    "valid_to":   "2026-07-01T00:00:00+00:00",
    "passed": true, "is_current": true, "corrects_id": null,
    "created_at": "2026-09-30T00:00:00+00:00"
  }
}
```

区间重叠的登记会被拒绝：`409 {"detail": {"code": "INTERVAL_OVERLAP", ...}}`。

### 2. 提交报告（放行成功）

```bash
curl -sS -X POST http://localhost:8000/reports \
  -H 'Content-Type: application/json' \
  -d '{
        "instrument": "HPLC-01",
        "tested_at": "2026-03-15T08:00:00Z",
        "reference": "R-2026-0001"
      }'
```

返回（`201`，含所据校准版本与判定快照）：

```json
{
  "released": true,
  "reason_code": "PASSED_CALIBRATION_COVERAGE",
  "basis_calibration": {"id": 1, "chain_id": 1, "version": 1, "passed": true, "...": "..."},
  "report_id": 1,
  "reference": "R-2026-0001",
  "snapshot": {
    "released": true,
    "reason_code": "PASSED_CALIBRATION_COVERAGE",
    "instrument": "HPLC-01",
    "tested_at": "2026-03-15T08:00:00+00:00",
    "basis_calibration": {"id": 1, "chain_id": 1, "version": 1, "passed": true}
  }
}
```

阻断场景（`422`，不落库）的 `detail.code`：

- `INSTRUMENT_DISABLED` —— 检测时间命中该仪器**未撤销**禁用区间（即使校准通过也拒绝，
  `detail.disable_basis` 给出事件号、区间与原因，左闭右开端点：`disabled_from` 命中、`disabled_to` 不命中）；
- `NO_CALIBRATION` —— 该仪器当前没有任何校准；
- `OUTSIDE_VALID_PERIOD` —— 有校准但检测时间不在任何左闭右开区间内（如恰等于 `valid_to`）；
- `CALIBRATION_NOT_PASSED` —— 覆盖该时点的校准结论为“未通过”。

### 2.1 登记禁用（仪器故障）

```bash
curl -sS -X POST http://localhost:8000/disables \
  -H 'Content-Type: application/json' \
  -d '{
        "instrument": "HPLC-01",
        "disabled_from": "2026-04-01T00:00:00Z",
        "disabled_to":   "2026-06-01T00:00:00Z",
        "reason": "进样泵故障"
      }'
```

返回（`201`，事件号 `id` 全局唯一）：

```json
{
  "disable_event": {
    "id": 1, "instrument": "HPLC-01",
    "disabled_from": "2026-04-01T00:00:00+00:00",
    "disabled_to":   "2026-06-01T00:00:00+00:00",
    "reason": "进样泵故障",
    "revoked": false, "revoked_at": null, "revoked_reason": null,
    "created_at": "2026-09-30T00:00:00+00:00"
  }
}
```

- `disabled_to` 传 `null` 或省略 = 持续禁用（开放区间）；
- `disabled_to <= disabled_from` -> `422 INVALID_INTERVAL`；
- 与同仪器未撤销区间重叠 -> `409 DISABLE_INTERVAL_OVERLAP`
  （错误体含 `conflicting_event_id`；边界相接如 `[..,6/1)+[6/1,..)` 允许）；
- 不同仪器的禁用区间互不影响。

此后禁用区间内提交报告：`422 INSTRUMENT_DISABLED`，

```json
{"detail": {
  "code": "INSTRUMENT_DISABLED",
  "message": "仪器 HPLC-01 在检测时间 ... 处于禁用状态（事件号 1，区间 [..)，左闭右开）：进样泵故障",
  "instrument": "HPLC-01",
  "tested_at": "2026-05-01T00:00:00+00:00",
  "disable_basis": {
    "event_id": 1, "instrument": "HPLC-01",
    "disabled_from": "2026-04-01T00:00:00+00:00",
    "disabled_to":   "2026-06-01T00:00:00+00:00",
    "reason": "进样泵故障"
  }
}}
```

### 2.2 撤销禁用与留痕查询

```bash
# 撤销（body 可省略；可附撤销备注）
curl -sS -X POST http://localhost:8000/disables/1/revoke \
  -H 'Content-Type: application/json' -d '{"reason": "泵修复并复核通过"}'

# 未知事件 404 NOT_FOUND；重复撤销 409 ALREADY_REVOKED
# 当前生效（未撤销）事件
curl -sS 'http://localhost:8000/disables?instrument=HPLC-01'
# 含已撤销的完整留痕（撤销后记录仍在）
curl -sS 'http://localhost:8000/disables?instrument=HPLC-01&include_revoked=true'
# 按事件号追溯单条
curl -sS http://localhost:8000/disables/1
```

撤销后同一检测时间恢复走校准判定；撤销本身不改写任何既有报告与快照。

### 2.3 按当前禁用状态查询受影响的已发布报告

```bash
curl -sS 'http://localhost:8000/disables/affected-reports?instrument=HPLC-01'
```

实时按**当前**未撤销禁用区间与已发布报告（`released=true`）的检测时间做连接，
命中即返回报告信息、发布时快照与 `disable_basis`。这是**视图式**结果：

- 事后追溯登记一个历史禁用区间 -> 当时已放行的报告立即出现在列表中；
- 撤销该事件 -> 列表立即不再包含这些报告；
- 报告记录本身始终保持 `released=true`，快照永不改写。

不带 `instrument` 时跨仪器返回全部受影响报告。

### 3. 更正校准（连续版本号 + 引用原记录）

```bash
curl -sS -X POST http://localhost:8000/calibrations/corrections \
  -H 'Content-Type: application/json' \
  -d '{
        "corrects_id": 1,
        "version": 2,
        "valid_from": "2026-02-01T00:00:00Z",
        "valid_to":   "2026-08-01T00:00:00Z",
        "passed": false
      }'
```

- 版本号必须是当前最新版 + 1，否则 `409 VERSION_CONFLICT`（响应中给出 `current_version`/`expected_version`）；
- 新区间与同仪器其他当前版本重叠时 `409 INTERVAL_OVERLAP`；
- 响应同时含 `affected_report_drift`：因本次更正导致“当前结论与发布快照不同”的报告列表。

更正后再发布同一检测时间：`422 CALIBRATION_NOT_PASSED`；而此前已发布报告的快照保持原值。

### 4. 重算漂移（不改写快照）

```bash
curl -sS 'http://localhost:8000/calibrations/drift/recompute?instrument=HPLC-01'
```

仅返回当前结论（放行/阻断或所据版本）与快照不同的报告，字段含 `change`：

- `NOW_BLOCKED` —— 快照放行，按当前校准已阻断；
- `NOW_COVERED_BY_OTHER_VERSION` —— 仍放行，但所据校准版本已变化；
- `NOW_RELEASEABLE` —— 快照阻断、当前可放行（历史阻断报告原本不落库，属防御性取值）。

### 脚本化完整演示

```bash
# 校准/更正流程
bash examples/curl-demo.sh                     # curl 版全流程
BASE_URL=http://localhost:8000 python3 examples/demo.py   # 含并发更正的 Python 版

# 仪器禁用流程（本轮新增）
bash examples/curl-disables.sh                          # curl 版全流程
BASE_URL=http://localhost:8000 python3 examples/demo_disables.py
    # 含“禁用登记 / 校准更正 / 报告提交”三方并发的 Python 版
```

## 并发正确性如何保证

1. **不会产生双最新版**
   - 唯一部分索引 `uq_calibration_one_current ON (chain_id) WHERE is_current`；
   - 更正先锁版本链行（`SELECT … FOR UPDATE`），再“旧版下线 + 新版插入”，同链并发更正天然串行；
   - 版本号非连续（基于过期版本提交）返回 `409 VERSION_CONFLICT`。
2. **当前区间不会重叠**：GiST 排他约束
   `EXCLUDE (instrument WITH =, tstzrange(valid_from, valid_to, '[)') WITH &&) WHERE (is_current)`，
   对并发插入同样生效，冲突映射为 `409 INTERVAL_OVERLAP`。
3. **不会依据过期校准放行**：发布与更正均为 SERIALIZABLE 事务；
   发布时锁定该仪器当前校准行（覆盖行 `FOR UPDATE`，无覆盖时锁全部当前行），
   与“下线旧版”的更正事务互斥；若重试后仍冲突，返回 `409 TRANSACTION_CONFLICT` 而非脏放行。
4. **未撤销禁用区间不会重叠**：`disable_events` 上同样有
   `EXCLUDE (instrument WITH =, tstzrange(disabled_from, disabled_to, '[)') WITH &&) WHERE (revoked_at IS NULL)`；
   登记先锁该仪器全部未撤销事件并做应用层预检（`409 DISABLE_INTERVAL_OVERLAP`），
   约束兜底并发插入；已撤销事件不参与约束，永久保留。
5. **不会在已生效禁用区间内误放行**：发布事务先做禁用判定——命中行 `FOR UPDATE`，
   未命中则锁定该仪器全部未撤销事件；与禁用登记/撤销事务互斥，
   且所有事务为 SERIALIZABLE（谓词锁覆盖“当时不存在命中行”的情况）。
   并发终态可串行：禁用生效后到达的提交只能得到 `422 INSTRUMENT_DISABLED`；
   禁用登记失败（重叠）不会影响任何在途判定。
6. **撤销的原子性**：撤销对事件行 `FOR UPDATE` 并以
   `UPDATE … WHERE revoked_at IS NULL` 条件置位，未知 `404`、重复撤销 `409`。

## 兼容性

- 原有校准与报告接口的请求体、成功响应结构与错误约定保持不变；新增能力均为新路径/新原因码。
- 数据库初始化仍在 API 启动时幂等执行；旧库直接重启即自动补齐 `disable_events` 表与约束，
  既有数据无需迁移。
