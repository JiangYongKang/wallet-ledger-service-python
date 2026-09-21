# 本地钱包账本服务（wallet-ledger-service）

用**本地模拟链事件流**代替真实节点的钱包账本服务。围绕一个目标设计：
充值确认、链重组回退、内部转账与提现在**任何入口**下得到的资金结论
**一致、守恒且可解释**。

- 框架：FastAPI（单进程内存态），金额一律使用整数最小单位（`CENT`）。
- 并发：所有"读-判定-写"在同一把可重入锁内串行，杜绝丢失更新、重复入账、负余额、半更新。
- 账本：**追加式（append-only）**条目 + 事件状态机，余额由账本投影得出，可审计回放。

---

## 1. 快速开始

```bash
uv sync
uv run uvicorn main:app --reload          # 启动服务
uv run pytest                             # 运行全部测试（会打印事件流水）
```

请求身份通过请求头传递（本地简化方案）：

| 头 | 含义 |
| --- | --- |
| `X-Principal-Id` | 调用方身份标识（必填） |
| `X-Principal-Role` | `user`（默认）或 `admin` |

写操作（转账/提现）必须带 `Idempotency-Key` 头。

---

## 2. HTTP 接口

### 用户侧（`/api/v1`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/wallets` | 注册钱包并绑定充值地址 |
| GET  | `/wallets/{id}/balance` | 余额视图（可用/在途/冻结/挂账分离） |
| POST | `/wallets/{id}/transfers` | 钱包间内部转账（需 `Idempotency-Key`） |
| POST | `/wallets/{id}/withdrawals` | 提现（先冻结 hold，再结算出库） |
| GET  | `/wallets/{id}/audit` | 事件流水 + 追加式账本条目（解释资金结论） |

### 管理侧（`/api/v1/admin`，仅 `admin`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/chain/blocks` | 在模拟链顶追加一个区块（含事件） |
| POST | `/chain/advance` | 空挖出块，推动确认数 |
| POST | `/chain/reorg` | 链重组，回退到指定高度 |
| POST | `/events/ingest` | 直接投喂一批原始事件（归一化后收录） |
| POST | `/scan` | 从链游标批量扫描、收录并推进确认（限流） |
| GET  | `/dead-letters` | 查看归一化失败/无法收录的事件及原因 |

---

## 3. 确认与最终性规则

设事件位于区块 `block_height`，当前链高为 `H`，区块埋深
`depth = H - block_height + 1`。配置项：

| 配置 | 默认 | 含义 |
| --- | --- | --- |
| `CONFIRMATIONS_REQUIRED` | 3 | `depth >= 3` 事件由 `pending` 转 `confirmed` |
| `FINALITY_DEPTH` | 6 | `depth > 6` 转 `finalized`，此后不可重组 |

事件状态机：

```
pending ──depth>=confirmations──▶ confirmed ──depth>finality_depth──▶ finalized
   │                                  │
   └──────────── reorg ───────────────┴──────────────▶ reverted（finalized 除外）
```

- **未达确认数**的充值金额只计入 `pending_in`，与 `available` 严格分离展示。
- 达到确认数后，充值才进入 `available`；链上转出事件（`out`）仅做记账核对，
  不会对已冻结/已扣的内部提现**重复扣款**。
- **最终化边界是确定的**：埋深 `== FINALITY_DEPTH` 仍可回退，`> FINALITY_DEPTH` 拒绝回退，
  返回 `finalized_reorg`（409）并携带 `event_ids`。整批回退中若混入已最终化事件，
  **整批原子拒绝**，不留下半更新。

### 重组回退如何保证不负余额、账目恒等

已确认充值被回退时，采用追加式冲账，拆成两类条目：

- `clawback`：从 `available` **实扣** `min(available, amount)`，恒有 `available >= 0`；
- `clawback_shortfall`：可用不足、无法实扣的部分记为**挂账 `clawback_due`**
  （对应资金已被转账/提现带走），不允许出现负余额。

后续新的充值达到确认数时，资金会**优先偿还挂账**（`clawback_repay`），
剩余部分才进入 `available`。

每个钱包满足恒等式：

```
available + holds + pending_in − clawback_due == 净本金（累计确认充值 − 累计出库/回退）
```

系统范围内把所有钱包求和，即得到"当前真实在系统内的本金"，测试中对每条操作序列都断言守恒。

**回退与确认同时到达**：确认投影短路逻辑规定 `reverted` 优先——一旦事件被标记
`reverted`，任何迟到/重放的确认都不会再入账；连续重放扫描结论完全一致、可复现。

### 余额字段

| 字段 | 含义 |
| --- | --- |
| `available` | 可用余额（可转账/可提现） |
| `pending_in` | 已收录但未达确认数的充值 |
| `holds` | 已发起提现、冻结待结算的金额 |
| `clawback_due` | 已确认充值被回退但余额不足形成的挂账 |

---

## 4. 幂等语义

- 所有资金写操作（转账、提现）必须携带 `Idempotency-Key`（缺失返回 422）。
- **相同 key + 相同语义**：重复请求返回**首次响应**，HTTP 200，
  响应体带 `"idempotent_replay": true`，不会重复扣款/入账。
- **相同 key + 不同载荷**（金额或目标不同）：返回 409 `idempotency_conflict`，明确拒绝。
- **请求处理中并发重入**（占位为 `in_flight`）：返回 409 `idempotency_conflict`。
- **业务性失败**（如余额不足 `insufficient_funds`）会释放占位，
  允许资金到位后用**同一个 key 重试**成功；它不是语义冲突。
- 链上事件以 `event_id` 幂等去重：重复事件返回 `duplicate` 不重复入账；
  同 `event_id` 但载荷不同返回 `conflict_state` 并进死信。
- 扫描游标在整批处理成功后才推进；重组后游标重置、从头重读，靠 `event_id` 去重，
  因此**上游失败重试不会产生重复入账**。

---

## 5. 身份与权限范围

- 钱包资源（余额、转账、提现、审计）只能由**属主** `X-Principal-Id` 访问。
- 越权访问返回 `404 not_found`，且与"钱包不存在"的响应**状态码和响应体完全一致、
  不回显 wallet_id**，无法据此推断目标资源是否存在。
- `/admin/**` 仅 `X-Principal-Role: admin` 可访问，越权返回 `403 permission_denied`。
- 缺少/非法身份返回 `401 unauthorized`。

---

## 6. 输入归一化与错误分类

投喂/扫描到的原始事件不会静默丢弃，也不会透出底层堆栈：

| reason code | 触发条件 |
| --- | --- |
| `missing_field` | 必填字段缺失/为空、条目不是对象 |
| `invalid_kind` | 类型不是 `deposit` / `out` |
| `invalid_amount` | 金额非数字、负数、0、非整数、NaN/Inf |
| `invalid_height` | 区块高度缺失或非正整数 |
| `invalid_tx` / `missing_field` | 交易哈希非法 / 为空 |
| `future_block` | 事件高度超过当前链高（乱序） |
| `unknown_address` | 地址未绑定任何钱包 |
| `conflict_state` | 同 `event_id` 不同载荷 |

所有失败条目进入 **dead-letter**（`GET /api/v1/admin/dead-letters`），
批量中的合法事件照常收录（部分成功）。链是区块高度的权威来源：上链时以真实高度为准，
防止"事件进了高区块却自报低高度"。

### 限流与积压

- 扫描接口使用令牌桶（`RATE_LIMIT_CAPACITY` / `RATE_LIMIT_REFILL_PER_SEC`），
  令牌不足返回 `429 rate_limited`，并在 `details.retry_after_ms` 给出建议退避时间。
- 单批受 `SCAN_MAX_EVENTS` 上界约束，积压需多批扫描，避免无界占用资源。
- 被限流时不推进游标、不产生副作用，调用方按 `retry_after_ms` 延迟重试即可。

---

## 7. 错误响应格式

```json
{ "error": { "code": "insufficient_funds", "message": "available 10 < required 50",
             "reason": null, "details": {} } }
```

`code` 可机读、可区分；未知异常统一收敛为 `500 internal_error`，细节仅入服务端日志。

---

## 8. 验证方法

```bash
uv run pytest          # 45+ 用例
```

测试重点（与需求对应）：

- `tests/test_reorg.py`：重组回退、已入账回撤、不足挂账、挂账偿还、回退幂等、
  回退与确认竞争的确定性、最终化原子拒绝。
- `tests/test_idempotency.py`：重复提交不重复扣款/入账、同 key 异载荷冲突、
  失败可重试、链事件与扫描重放去重。
- `tests/test_auth.py`：越权 404 与不存在不可区分、管理端 403、缺身份 401。
- `tests/test_concurrency.py`：多线程并发转账/提现/确认/回退，断言无丢失更新、
  不重复入账、不负余额、资金守恒。
- `tests/test_ingestion_errors.py`：缺字段/乱序/非法金额的可区分分类与死信。
- `tests/test_scan.py`：批量分批、限流退避、重试幂等、批大小有界。
- `tests/test_invariant.py`：交错操作下的全系统资金守恒与审计快照确定性。

每个用例结束时打印钱包维度的**事件流水、追加式账本条目和余额投影**，
日志中可直接核对每条资金结论的判定依据。

### 手动串演示例

```bash
# 1) 用户建钱包
curl -s -XPOST localhost:8000/api/v1/wallets \
  -H 'X-Principal-Id: u1' -H 'Content-Type: application/json' \
  -d '{"address":"0xAAA"}'

# 2) 管理员在高度1放入一笔充值事件并直接摄入
curl -s -XPOST localhost:8000/api/v1/admin/chain/blocks \
  -H 'X-Principal-Id: ops' -H 'X-Principal-Role: admin' -H 'Content-Type: application/json' \
  -d '{"events":[{"event_id":"ev1","address":"0xAAA","kind":"deposit","amount":100,"tx_hash":"0xt1"}]}'
curl -s -XPOST localhost:8000/api/v1/admin/events/ingest \
  -H 'X-Principal-Id: ops' -H 'X-Principal-Role: admin' -H 'Content-Type: application/json' \
  -d '{"events":[{"event_id":"ev1","address":"0xAAA","kind":"deposit","amount":100,"block_height":1,"tx_hash":"0xt1"}]}'

# 3) 推进到高度3，达到3确认 -> available=100
curl -s -XPOST localhost:8000/api/v1/admin/chain/advance \
  -H 'X-Principal-Id: ops' -H 'X-Principal-Role: admin' -H 'Content-Type: application/json' \
  -d '{"blocks":2}'

# 4) 回退到高度1，查看冲账流水与余额
curl -s -XPOST localhost:8000/api/v1/admin/chain/reorg \
  -H 'X-Principal-Id: ops' -H 'X-Principal-Role: admin' -H 'Content-Type: application/json' \
  -d '{"height":1}'
```
