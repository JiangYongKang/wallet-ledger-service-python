# 本地钱包账本服务（本地模拟链上事件流）

一个不依赖真实链节点的钱包账本服务：用本地模拟的链上事件流（充值 / 链上转出、
确认数推进、链重组回退）驱动账本，使**充值确认、链重组回退、转账与提现在各入口下
的资金结论一致、确定且可解释**。现已支持**同一钱包下多资产（多币种）分口径记账**：
资金变动只落请求指定的资产，不同资产之间互不抵扣、互不串账；不带资产信息的旧请求
按默认资产处理，单资产时代的用法与响应形状保持不变。

- 语言/框架：Python 3.14 + FastAPI，纯内存状态（确定性、易复现、便于并发测试）
- 金额单位：一律使用**最小货币单位的非负整数**，杜绝浮点误差
- 资产标识：可选 `asset`（如 `USDT`/`BTC`），服务端归一化为**大写**，
  仅允许字母/数字/`-`/`_`、长度 1–16，防止“同一币种不同写法”造成串账；
  缺省统一落账到默认资产 `DEFAULT`

## 运行与验证

```bash
uv sync                       # 安装依赖
uv run pytest                 # 全部单测（资金守恒 / 重组 / 幂等 / 越权 / 并发）
uv run pytest -s              # 同时打印事件流水与判定依据日志
uv run uvicorn main:app --reload   # 启动 HTTP 服务
```

演示主体（请求头 `X-Principal: principal:<id>`）：

| 主体 | 角色 | 可访问钱包 |
| --- | --- | --- |
| alice | user | wA, wB |
| bob | user | wC |
| indexer | indexer | 链事件投喂 / 扫描 |
| admin | admin | 管理角色 |

## HTTP 接口

| 方法 & 路径 | 角色 | 说明 |
| --- | --- | --- |
| `POST /wallets` | user | 在自身钱包范围内创建/认领钱包（幂等） |
| `GET /wallets/{addr}/balance` | user | 单资产分口径余额（可用/待入账/回撤平账），`?asset=` 可选，缺省默认资产 |
| `GET /wallets/{addr}/balances` | user | 一次返回该钱包下**每个资产**各自的分口径余额 |
| `GET /wallets/{addr}/entries` | user | 只追加的资金流水（含资产标记与判定 memo），`?asset=` 可选过滤 |
| `POST /ledger/transfer` | user | 钱包内转账（需 `idempotency_key`，可选 `asset`，缺省默认资产） |
| `POST /ledger/withdraw` | user | 提现，即时扣该资产可用（需 `idempotency_key`，可选 `asset`） |
| `POST /chain/events` | indexer | 投喂单个模拟链事件（宽松入模 + 归一化，可选 `asset`） |
| `POST /chain/scan?wait=` | indexer | 批量确认扫描 + 重组回撤，**一批可混多资产**，受批量/限流约束 |

### 快速演练

```bash
H='X-Principal: principal:alice'; I='X-Principal: principal:indexer'
curl -s -XPOST localhost:8000/wallets -H "$H" -d '{"address":"wA"}' -H 'Content-Type: application/json'
curl -s -XPOST localhost:8000/chain/events -H "$I" -H 'Content-Type: application/json' \
  -d '{"tx_hash":"t1","log_index":0,"type":"deposit","address":"wA","amount":"100","block_height":10}'
curl -s "$H" localhost:8000/wallets/wA/balance   # available=0 pending_in=100
curl -s -XPOST localhost:8000/chain/scan -H "$I" -H 'Content-Type: application/json' \
  -d '{"tip_height":13,"events":[{"tx_hash":"t1","log_index":0,"confirmations":3}]}'
curl -s "$H" localhost:8000/wallets/wA/balance   # available=100 pending_in=0
```

多资产演练（默认资产与 USDT/BTC 各自独立）：

```bash
# 两种资产的充值可以在同一批扫描里混着确认
curl -s -XPOST localhost:8000/chain/events -H "$I" -H 'Content-Type: application/json' \
  -d '{"tx_hash":"u1","log_index":0,"type":"deposit","address":"wA","amount":"100","block_height":10,"asset":"USDT"}'
curl -s -XPOST localhost:8000/chain/events -H "$I" -H 'Content-Type: application/json' \
  -d '{"tx_hash":"b1","log_index":0,"type":"deposit","address":"wA","amount":"5","block_height":10,"asset":"BTC"}'
curl -s -XPOST localhost:8000/chain/scan -H "$I" -H 'Content-Type: application/json' \
  -d '{"tip_height":13,"events":[{"tx_hash":"u1","log_index":0,"confirmations":3},{"tx_hash":"b1","log_index":0,"confirmations":3}]}'
# 一次看清每个资产各自的分口径余额
curl -s "$H" localhost:8000/wallets/wA/balances
#   {"address":"wA","assets":{"BTC":{...,"available":5},"USDT":{...,"available":100}}}
# BTC 的余额不能用于 USDT 提现（INSUFFICIENT_FUNDS，details 带 asset）
curl -s -XPOST localhost:8000/ledger/withdraw -H "$H" -H 'Content-Type: application/json' \
  -d '{"address":"wA","amount":100,"idempotency_key":"w1","asset":"BTC"}'
```

## 核心规则

### 0. 多资产（多币种）记账语义

- 每个钱包地址下**按资产分别持有一套分口径余额**（`available` / `pending_in` /
  `pending_out` / `reorg_debt`）。资产是链事件与用户写操作的显式维度，
  充值确认、链上提现、内部转账、提现与链上回退带来的资金变动，**只落到
  请求里指定的那个资产上**；不同资产之间不能互相抵扣、垫付或串账。
- 资产标识大小写不敏感，服务端统一归一化为大写（`usdt`、`UsDT` 与 `USDT`
  是同一个资产），非法字符/超长/类型错误返回 **400 `INVALID_ASSET`**。
- 查询：
  - `GET /wallets/{addr}/balances` 一次返回该钱包下**每个资产**各自的四口径；
  - `GET /wallets/{addr}/balance?asset=USDT` 查指定资产；不带 `asset` 查默认资产；
  - 从未发生过某资产变动时，该资产返回全 0，但不会凭空落一条空账。
- 流水只追加、每条带 `asset` 标记，可按 `?asset=` 过滤；损失汇账户的平账流水
  同样带资产标记，资金去向可解释。
- 幂等指纹包含 `asset`：同一幂等键对不同资产（或不同金额/方向）提交属于
  **语义冲突**（409 `IDEMPOTENCY_CONFLICT`），不会从 A 资产扣款回放成 B 资产。
- 并发隔离仍以**钱包地址**为锁粒度：同一钱包下不同资产的写操作在同一临界区内
  串行，因此资产之间不会丢更新、不会出现半更新（见并发测试）。
- 批量扫描允许一批混着多种资产的事件；确认/回撤逐条按事件自身资产落账，
  令牌桶按**事件数**而不是资产种类数计费，批量上限（`max_batch_size`）、
  在途上限与 `wait/429+retry_after` 策略均不因币种增多而改变。

**默认资产的兼容范围**：所有写接口与链事件的 `asset` 字段都可选，缺省（或空串）
一律按默认资产 `DEFAULT` 处理。因此单资产时代不带 `asset` 的请求体、幂等键、
资金结论与**默认资产的响应字段形状（不包含 `asset` 键）**完全不变；默认资产与
任何显式命名资产之间同样互不抵扣。

### 1. 确认数与最终性

- 每个充值事件以 `(tx_hash, log_index)` 为**自然键**（资产是该事件的固有属性），
  状态机：`PENDING → CONFIRMED → FINALIZED`，任一时点可转入终态 `REORGED`。
- 事件投喂后计入**该资产**的 `pending_in`（待入账），**不进入可用余额**。
- 当 `confirmations >= required_confirmations`（默认 **3**）时达到最终性，
  金额在该资产内从 `pending_in` 转入 `available`（可用余额），两口径分开返回。
- `confirmations >= finalization_depth`（默认 **64**）标记为 FINALIZED，
  此后**不可被重组回撤**（回撤请求显式忽略，不静默丢弃）。
- 每个资产各自有四个口径：
  - `available`：该资产可用余额（已最终性入账 − 转出/提现/回撤扣回）
  - `pending_in`：该资产未达确认数的入账
  - `pending_out`：该资产未广播链上提现在途（本期保留为 0）
  - `reorg_debt`：该资产回撤时可用不足而平账到系统“损失汇账户”的累计额

### 2. 链重组 / 事件回退（回撤优先，结论确定）

- 同一批次（或并发同时到达）中确认与回撤都到达时，**先执行回撤，再执行确认**；
  已回撤事件上的确认一律忽略。因此无论到达顺序/线程交错，结论唯一可复现。
- 回撤与确认的资金动作只在**事件所属资产**内发生；同一批混多资产时逐资产独立。
- 未确认充值回撤：仅回退该资产 `pending_in`。
- 已确认充值回撤：从该资产 `available` 扣回；若可用不足（该资产的钱已转出/
  提现），扣到 **0**，缺口计入**该资产**的 `reorg_debt` 并追加一条带资产标记的
  `@loss-sink` 平账流水，**该资产余额永不为负**，其他资产不受影响，
  资金去向在流水里可解释。
- 链上转出事件（withdrawal）若被回撤，资金回到该资产可用余额。
- 回撤、确认、扫描都带幂等闸门（`confirmed/reorged/finalized`），重复投递安全。

**资金守恒不变式（对每个资产独立成立）**：

```
Σ wallet.available[asset] − Σ wallet.reorg_debt[asset]
  = 该资产净外部注入(已确认未回撤充值 − 链上提现)
```

内部转账只是某资产的 `available` 在钱包间搬移，不改变该资产总量；跨资产之间
不存在任何搬移或抵扣。`reorg_debt` 是系统吸收“已花出却被回撤的无背书资金”
的损失汇账户，因此以负号并入恒等式。

### 3. 幂等语义（重复 vs 冲突可区分）

- 用户写操作必须携带 `idempotency_key`。服务端对请求体做规范化指纹（SHA-256），
  指纹包含 `asset` 字段。
  - **同键 + 同体（重复提交）**：不重复扣款/入账，回放原结果，
    HTTP 返回 **208** 且响应带 `"replay": true`（与首次 200 可区分）。
  - **同键 + 不同体（语义冲突，包括资产不同）**：返回
    **409 `IDEMPOTENCY_CONFLICT`**，拒绝执行。
  - 幂等键按主体隔离命名空间（`principal_id::key`）。
  - 余额不足等业务失败发生在写入幂等记录之前，因此不会“污染”该键。
- 链事件以自然键去重：重复投喂返回 **409 `DUPLICATE_EVENT`**，不重复增加待入账。
- 确认/回撤/扫描由状态机幂等：失败重试不会重复入账。

### 4. 身份、权限与资源存在性

- 缺失/无法识别凭据 → **401 `UNAUTHENTICATED`**。
- 已认证但角色不符（如 user 调链入口）→ **403 `FORBIDDEN`**。
- 访问/操作**不在自身钱包集合**的地址，与“地址不存在”返回**完全相同**的
  **404 `RESOURCE_NOT_FOUND`**（同状态码、同 code、同文案），
  不泄露目标资源是否存在；越权目标（含转账收款方）同样 404。

### 5. 并发一致性

- 每个钱包地址一把 `RLock`；跨钱包写操作（转账）按地址排序后依次加锁，避免死锁。
- 同一钱包内**任意资产**的入账/确认/回撤/转出/提现在同一临界区内串行完成
  余额与流水更新：不同资产不会相互干扰，杜绝丢失更新、重复入账、负余额与半更新。
- 并发测试（`tests/test_concurrency.py`、`tests/test_multi_asset.py`）
  用多线程 + Barrier 制造竞争，断言“各资产恰好一次生效、绝不负余额、
  流水唯一、逐资产资金守恒”。

### 6. 模拟链输入归一化与错误分类

所有错误返回统一结构 `{code, message, details}`，错误码互不相同、可程序化区分：

| code | 状态 | 触发 |
| --- | --- | --- |
| `MISSING_FIELD` | 400 | 必填字段缺失或为空（含空字符串） |
| `INVALID_AMOUNT` | 400 | 金额为 0/负/小数/浮点/非数字/布尔 |
| `INVALID_ASSET` | 400 | 资产标识非字符串、含非法字符或超长 |
| `INVALID_EVENT` | 400 | 未知事件类型、tx_hash/address 非法等 |
| `OUT_OF_ORDER` | 422 | 扫描 tip 高度回退（乱序，不静默丢弃） |
| `DUPLICATE_EVENT` | 409 | 链事件自然键重复 |
| `DUPLICATE_REQUEST` | 409 | 重复请求（另有 208 重放路径） |
| `IDEMPOTENCY_CONFLICT` | 409 | 同键不同体 |
| `INSUFFICIENT_FUNDS` | 409 | 可用余额不足 |
| `UNAUTHENTICATED` / `FORBIDDEN` / `RESOURCE_NOT_FOUND` | 401/403/404 | 鉴权与越权 |
| `RATE_LIMITED` | 429 | 令牌桶/在途/批量超限，带 `retry_after` |

底层异常不会透出给调用方；链输入问题在归一化层被转换为上述领域错误。

### 7. 批量确认扫描与限流

- 扫描按 `tip_height` 单调推进，回退的 tip 抛 `OUT_OF_ORDER`（可由上游修正后重试）。
- 单批事件数受 `max_batch_size`（默认 100）约束，超限显式拒绝（不截断、不落半批）；
  即使一批内全部是不同资产，约束口径仍为事件数。
- `RateLimiter` 组合**令牌桶**（吞吐/突发）与**在途计数**（并发上限）：
  积压时可用 `POST /chain/scan?wait=<seconds>` 让请求等待令牌，
  否则立即返回 429 与建议的 `retry_after`，避免无界占用资源；
  令牌按事件数扣减，**资产种类增多不会放大资源占用**。
- 扫描内部对事件按自然键排序、先回撤后确认，处理结果确定；事件携带什么资产
  就落什么资产，一批可安全混多资产；下游状态机幂等，上游失败重试不重复入账。

## 目录结构

```
wallet_ledger/
  domain.py        领域类型（金额整数、默认资产、按资产分口径余额、事件状态机、幂等记录）
  errors.py        可区分错误码与 HTTP 状态映射
  repository.py    内存状态容器 + 每钱包锁 + 按资产标记的只追加流水
  service.py       资金结论权威：投喂/确认/回撤/转账/提现/幂等/守恒（逐资产）
  normalizer.py    模拟链输入与资产标识归一化、分类报错
  idempotency.py   请求指纹（含资产）+ 幂等键命名空间
  security.py      认证 / 角色 / 钱包归属授权（不泄露存在性）
  scanner.py       批量确认扫描（可混多资产）、回撤优先、乱序策略
  ratelimit.py     令牌桶 + 在途限流（按事件数计费）
  audit.py         事件流水与判定依据日志
  config.py        确认数 / 最终性 / 限流配置
  api/             FastAPI 装配、依赖、schema 与路由
tests/             重组回退 / 重复提交 / 越权 / 并发 / 归一化 / HTTP 集成 / 多资产
```

## 测试覆盖与验证方法

```bash
uv run pytest                      # 全部用例（单资产旧用例 + 多资产新增用例）
uv run pytest -s                   # 打印每个判定的事件流水（INFO 日志）
uv run pytest tests/test_reorg.py        # 重组/回撤/守恒/回撤优先
uv run pytest tests/test_idempotency.py  # 重复提交 / 语义冲突 / 键隔离
uv run pytest tests/test_permissions.py  # 角色 / 越权 404 同构
uv run pytest tests/test_concurrency.py  # 并发：恰好一次、不负、守恒
uv run pytest tests/test_multi_asset.py  # 多资产：逐资产守恒/回撤/幂等/并发隔离/兼容
uv run pytest tests/test_normalizer.py   # 缺字段/非法金额/乱序/批量
uv run pytest tests/test_http_integration.py  # 端到端 + 429 + 208 重放
```

`tests/test_multi_asset.py` 的断言重点：

- **逐资产资金守恒**：充值确认、内部转账、提现只落指定资产；各资产
  `Σavailable − Σreorg_debt = 净外部注入` 独立成立，默认资产恒为 0 不受干扰。
- **回撤与平账**：某资产已花出后回撤只把该资产钳 0、缺口计入该资产
  `reorg_debt` 并产生带资产标记的 `@loss-sink` 流水，其他资产不动；
  混资产批次里“回撤优先”仍逐资产确定。
- **幂等**：同键同资产重放 208 且只扣一次；同键不同资产判为冲突且不落任何账；
  混资产扫描重试不重复确认。
- **并发隔离**：同一钱包多资产并发提现/转账/确认，各资产恰好一次生效、
  绝不负余额、流水 ref_key 唯一；同资产同键并发仍恰好一次。
- **兼容**：完全不带 `asset` 的旧请求按默认资产处理，余额响应仍是旧字段形状；
  非法资产以 `INVALID_ASSET`(400) 拒绝且事件不入模。
- **容量**：混多资产批量的令牌消耗只与事件数有关；超 `max_batch_size`
  即使全部是不同资产也整体 429 拒绝、不做半批处理。

测试断言重点为**资金守恒、余额不为负、错误类型可区分、恰好一次语义**；
服务在关键判定（待入账、确认、回撤、缺口平账、重复回放、冲突拒绝）处通过
`wallet_ledger.*` logger 打印事件流水与依据，可用 `caplog` 或 `-s` 查看。
