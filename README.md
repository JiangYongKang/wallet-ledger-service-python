# 本地钱包账本服务（本地模拟链上事件流）

一个不依赖真实链节点的钱包账本服务：用本地模拟的链上事件流（充值 / 链上转出、
确认数推进、链重组回退）驱动账本，使**充值确认、链重组回退、转账与提现在各入口下
的资金结论一致、确定且可解释**。

- 语言/框架：Python 3.14 + FastAPI，纯内存状态（确定性、易复现、便于并发测试）
- 金额单位：一律使用**最小货币单位的非负整数**，杜绝浮点误差
- **多资产**：账本按 `(钱包地址, 资产)` 维度独立记账；不带资产信息的旧请求
  一律按默认资产 `COIN` 处理，单资产用法完全不变

## 运行与验证

```bash
uv sync                       # 安装依赖
uv run pytest                 # 全部单测（资金守恒 / 重组 / 幂等 / 越权 / 并发 / 多资产）
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
| `GET /wallets/{addr}/balance` | user | 钱包级余额：顶层为默认资产口径，`assets` 为全部资产的分口径；可带 `?asset=BTC` 只查单资产 |
| `GET /wallets/{addr}/entries` | user | 只追加的资金流水（含判定 memo、每条带 `asset`），可带 `?asset=` 过滤 |
| `POST /ledger/transfer` | user | 钱包内转账（需 `idempotency_key`，可选 `asset`，缺省 `COIN`） |
| `POST /ledger/withdraw` | user | 提现，即时扣可用（需 `idempotency_key`，可选 `asset`，缺省 `COIN`） |
| `POST /chain/events` | indexer | 投喂单个模拟链事件（宽松入模 + 归一化，可选 `asset`，缺省 `COIN`） |
| `POST /chain/scan?wait=` | indexer | 批量确认扫描 + 重组回撤，受批量/限流约束；**一批可混多种资产** |

### 快速演练

```bash
H='X-Principal: principal:alice'; I='X-Principal: principal:indexer'
curl -s -XPOST localhost:8000/wallets -H "$H" -d '{"address":"wA"}' -H 'Content-Type: application/json'
curl -s -XPOST localhost:8000/chain/events -H "$I" -H 'Content-Type: application/json' \
  -d '{"tx_hash":"t1","log_index":0,"type":"deposit","address":"wA","amount":"100","block_height":10}'
curl -s "$H" localhost:8000/wallets/wA/balance   # available=0 pending_in=100（默认资产 COIN）
curl -s -XPOST localhost:8000/chain/scan -H "$I" -H 'Content-Type: application/json' \
  -d '{"tip_height":13,"events":[{"tx_hash":"t1","log_index":0,"confirmations":3}]}'
curl -s "$H" localhost:8000/wallets/wA/balance   # available=100 pending_in=0
# 多资产：BTC 充值/确认，与 COIN 完全分账
curl -s -XPOST localhost:8000/chain/events -H "$I" -H 'Content-Type: application/json' \
  -d '{"tx_hash":"tb1","log_index":0,"type":"deposit","address":"wA","amount":"200","block_height":10,"asset":"BTC"}'
curl -s "$H" 'localhost:8000/wallets/wA/balance?asset=BTC'   # 只看 BTC
```

## 多资产记账语义

- **记账维度**：所有余额（`available` / `pending_in` / `pending_out` /
  `reorg_debt`）都按 `(钱包地址, 资产)` 分别维护，流水只追加且每条带 `asset`。
  充值确认、链上转出/回退、内部转账、提现一律落到请求指定的资产上，
  **不同资产之间不互相抵扣、不串账**：某资产余额不足时显式
  `INSUFFICIENT_FUNDS`（details 带 `asset`），绝不动用其他资产。
- **资产符号**：可选字符串，去空白后**统一大写**并按
  `^[A-Z0-9][A-Z0-9._-]{0,31}$` 校验；缺失/为 `null` → 默认资产 `COIN`，
  空串或非法字符 → 400 `INVALID_ASSET`。因此 `"btc"` 与 `"BTC"` 是同一本账。
- **默认资产兼容范围**：
  - 旧的链事件投喂、确认扫描、`/ledger/transfer`、`/ledger/withdraw` 请求体
    不带 `asset` 字段时，全部记到 `COIN`，结论与升级前一致；
  - `GET /wallets/{addr}/balance` 的**顶层字段仍是 COIN 口径**，旧客户端无感；
    新客户端读新增的 `assets` 映射即可一次拿到全部资产；
  - 服务层 `get_balance / transfer / withdraw` 的旧位置参数签名保持不变。
- **链事件自然键**仍是 `(tx_hash, log_index)`（链上该键唯一对应一笔事件及其
  资产）；重复投喂（即便声称不同资产）一律 409 `DUPLICATE_EVENT`。
- **幂等**：`asset`（归一化后的符号）纳入请求指纹，因此同一幂等键
  同资产重复提交 → 208 安全回放；**同键换资产 → 409 `IDEMPOTENCY_CONFLICT`**，
  重复与语义冲突仍可区分。
- **确认数/最终性口径不变**：每个资产各自走 `PENDING → CONFIRMED →
  FINALIZED`；某资产事件达到最终性后不可回撤，不影响同钱包其他资产的回撤。
- **回退平账按资产独立**：已确认充值回撤只扣该资产 `available`，不足部分计入
  **该资产** 的 `reorg_debt` 并追加带资产标记的 `@loss-sink` 流水；任何资产
  余额都不会为负。链上转出事件被回撤时，只把当初真正扣掉的部分退回**原资产**
  可用余额，当初的不足缺口从**原资产** `reorg_debt` 冲销，其他资产分文不动。
- **并发隔离**：仍按钱包地址加锁，同一钱包下不同资产的写操作在同一临界区串行，
  互不丢更新、无半更新；跨钱包转账仍按地址排序加锁避免死锁。
- **容量**：扫描限流与批量上限按**事件条数**计算（令牌/在途/`max_batch_size`
  与单资产时完全一致），一批混多种资产不会按资产数放大资源占用；
  该等待（`wait`）等待、该拒绝（429 带 `retry_after`）就拒绝。
- **守恒不变式对每个资产分别成立**：

```
Σ wallet.available(asset) − Σ wallet.reorg_debt(asset)
  = 该资产净外部注入（已确认未回撤充值 − 链上转出）
```

## 核心规则

### 1. 确认数与最终性

- 每个充值事件以 `(tx_hash, log_index)` 为**自然键**，状态机：
  `PENDING → CONFIRMED → FINALIZED`，任一时点可转入终态 `REORGED`。
- 事件投喂后计入该资产的 `pending_in`（待入账），**不进入可用余额**。
- 当 `confirmations >= required_confirmations`（默认 **3**）时达到最终性，
  金额从该资产的 `pending_in` 转入 `available`（可用余额），两口径分开返回。
- `confirmations >= finalization_depth`（默认 **64**）标记为 FINALIZED，
  此后**不可被重组回撤**（回撤请求显式忽略，不静默丢弃）。
- 每个资产各自维护三 + 一口径（`GET /wallets/{addr}/balance` 的 `assets`
  映射可一次取全，`?asset=` 只取单个资产）：
  - `available`：可用余额（已最终性入账 − 转出/提现/回撤扣回）
  - `pending_in`：未达确认数的入账
  - `pending_out`：未广播链上提现在途（本期保留为 0）
  - `reorg_debt`：回撤时可用不足而平账到系统“损失汇账户”的累计额

### 2. 链重组 / 事件回退（回撤优先，结论确定）

- 同一批次（或并发同时到达）中确认与回撤都到达时，**先执行回撤，再执行确认**；
  已回撤事件上的确认一律忽略。因此无论到达顺序/线程交错，结论唯一可复现。
- 未确认充值回撤：仅回退 `pending_in`。
- 已确认充值回撤：从 `available` 扣回；若可用不足（钱已转出/提现），
  扣到 **0**，缺口计入 `reorg_debt` 并追加一条 `@loss-sink` 平账流水，
  **余额永不为负**，资金去向在流水里可解释。
- **链上转出事件回撤：恢复到这笔转出发生之前**。链上转出是既成事实，投喂时
  从该资产 `available` 实扣（不足则扣到 0），超出可用的部分计入**该资产**
  `reorg_debt`；事件记录上留存当时的拆分：`debited_amount`（真正从可用
  扣掉的金额）与 `shortfall_amount`（因不足而平账的缺口）。回撤时：
  - **只把 `debited_amount` 退回 `available`**，绝不按整笔金额回退
    （否则缺口部分会凭空抬高可用，造成“本来没钱的地址回退后反而能花”）；
  - **`shortfall_amount` 一并从 `reorg_debt` 冲销**，并在 `@loss-sink`
    追加一条 `reorg_shortfall_reversal` 反向流水，与当初的平账流水一一对应；
  - 回退只作用于该事件自己的资产桶，不同资产互不抵扣、互不串账；
    任一资产 `available` 既不因回退变负，也不被虚增，分口径余额与流水可对账。
- 回撤、确认、扫描都带幂等闸门（`confirmed/reorged/finalized`），重复投递安全；
  重复回撤不重复退款/冲销，回撤后再到的确认一律忽略。

**资金守恒不变式（对每个资产分别成立）**：

```
Σ wallet.available(asset) − Σ wallet.reorg_debt(asset) = 该资产净外部注入(已确认未回撤充值 − 链上提现)
```

内部转账只是某资产的 `available` 在钱包间搬移，不改变该资产总量；跨资产
之间不做任何自动兑换。`reorg_debt` 是系统吸收“已花出却被回撤的无背书资金”
的损失汇账户（按资产分别累计），因此以负号并入恒等式。被回撤的链上转出
在恒等式两侧同时消失（退款回 `available`、缺口从 `reorg_debt` 冲销），
即该笔转出视同从未发生。

### 3. 幂等语义（重复 vs 冲突可区分）

- 用户写操作必须携带 `idempotency_key`。服务端对请求体做规范化指纹（SHA-256）。
  - **同键 + 同体（重复提交）**：不重复扣款/入账，回放原结果，
    HTTP 返回 **208** 且响应带 `"replay": true`（与首次 200 可区分）。
  - **同键 + 不同体（语义冲突）**：返回 **409 `IDEMPOTENCY_CONFLICT`**，拒绝执行。
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
- 同一钱包的入账/确认/回撤/转出/提现在临界区内串行完成余额与流水更新，
  不同资产也不例外（资产桶独立，但受同一钱包锁保护），
  杜绝丢失更新、重复入账、负余额与半更新。
- 并发测试（`tests/test_concurrency.py` 用单资产制造高强度竞争；
  `tests/test_multi_asset.py` 验证不同资产并发时的隔离）
  用多线程 + Barrier 制造竞争，
  断言"恰好一次生效、绝不负余额、流水唯一、资金守恒（逐资产）"。

### 6. 模拟链输入归一化与错误分类

所有错误返回统一结构 `{code, message, details}`，错误码互不相同、可程序化区分：

| code | 状态 | 触发 |
| --- | --- | --- |
| `MISSING_FIELD` | 400 | 必填字段缺失或为空（含空字符串） |
| `INVALID_AMOUNT` | 400 | 金额为 0/负/小数/浮点/非数字/布尔 |
| `INVALID_ASSET` | 400 | 资产符号非法（空串、非字符串、字符集或长度非法） |
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
- 单批事件数受 `max_batch_size`（默认 100）约束，超限显式拒绝（不截断）。
- `RateLimiter` 组合**令牌桶**（吞吐/突发）与**在途计数**（并发上限）：
  积压时可用 `POST /chain/scan?wait=<seconds>` 让请求等待令牌，
  否则立即返回 429 与建议的 `retry_after`，避免无界占用资源。
- 扫描内部对事件按自然键排序、先回撤后确认，处理结果确定；
  单批可同时包含多种资产的事件，各自落到自己的资产桶，限流仍按事件条数计；
  下游状态机幂等，上游失败重试不产生重复入账。

## 目录结构

```
wallet_ledger/
  domain.py        领域类型（金额整数、事件状态机、默认资产、(钱包,资产)余额、身份）
  errors.py        可区分错误码与 HTTP 状态映射（含 INVALID_ASSET）
  repository.py    内存状态容器（address→asset→Balance 资产桶）+ 每钱包锁 + 只追加流水
  service.py       资金结论权威：投喂/确认/回撤/转账/提现/幂等/守恒，按资产落账
  normalizer.py    模拟链输入归一化（含资产符号大写归一）与分类报错
  idempotency.py   请求指纹（含资产）+ 幂等键命名空间
  security.py      认证 / 角色 / 钱包归属授权（不泄露存在性）
  scanner.py       批量确认扫描（一批可混多资产）、回撤优先、乱序策略
  ratelimit.py     令牌桶 + 在途限流（按事件条数，不按资产数放大）
  audit.py         事件流水与判定依据日志
  config.py        确认数 / 最终性 / 限流配置
  api/             FastAPI 装配、依赖、schema 与路由
tests/             重组回退 / 重复提交 / 越权 / 并发 / 归一化 / HTTP 集成 / 多资产
```

## 测试覆盖与验证方法

```bash
uv run pytest                      # 全部 82 个用例（原单资产用例 + 多资产用例）
uv run pytest -s                   # 打印每个判定的事件流水（INFO 日志）
uv run pytest tests/test_reorg.py        # 重组/回撤/守恒/回撤优先/链上转出回撤
uv run pytest tests/test_idempotency.py  # 重复提交 / 语义冲突 / 键隔离
uv run pytest tests/test_permissions.py  # 角色 / 越权 404 同构
uv run pytest tests/test_concurrency.py  # 并发：恰好一次、不负、守恒
uv run pytest tests/test_multi_asset.py  # 多资产：独立记账/守恒、回退与幂等、并发隔离、HTTP
uv run pytest tests/test_normalizer.py   # 缺字段/非法金额/乱序/批量
uv run pytest tests/test_http_integration.py  # 端到端 + 429 + 208 重放
```

链上转出回撤相关用例（`tests/test_reorg.py` 单资产、
`tests/test_multi_asset.py` 多资产）稳定复现并锁定以下边界：

- **可用充足时的链上转出被回退**：整笔实扣退回可用，无缺口冲销流水；
- **可用不足（含该资产零余额）的链上转出被回退**：只退实扣部分、冲销缺口，
  可用与缺口都恢复到转出之前（零余额地址回退后仍是 0/0，不能再转出/提现）；
- **重复回退、回退后再确认**：幂等闸门保证不重复退款/冲销，结论唯一；
- **回退与确认同批到达**：先回退后确认（确认被忽略），重放同批结论一致；
- **多资产隔离**：BTC 链上转出的扣款/缺口/回退只动 BTC，ETH、COIN 分文不动，
  缺口平账与其冲销流水都带 BTC 资产标记。

本地手工验证（零余额地址回退后不应出现可花余额）：

```bash
uv run python - <<'EOF'
from wallet_ledger.repository import LedgerRepository
from wallet_ledger.service import LedgerService
from wallet_ledger.domain import PrincipalRole
from wallet_ledger.security import register_principal
from wallet_ledger.normalizer import normalize_event

s = LedgerService(LedgerRepository())
idx = register_principal("indexer", PrincipalRole.INDEXER, set())
alice = register_principal("alice", PrincipalRole.USER, {"wA"})
s.create_wallet(alice, "wA")
ev = normalize_event({"tx_hash": "w1", "log_index": 0, "type": "withdrawal",
                      "address": "wA", "amount": "100", "block_height": 12})
w = s.ingest_event(idx, ev)                  # 零余额超额转出：available=0 debt=100
s.apply_reorg(idx, [w.event.natural_key], 13)
print(s.get_balance(alice, "wA").to_dict())  # 修复后：available=0 reorg_debt=0
EOF
```

多资产用例（`tests/test_multi_asset.py`）重点验证：

- **独立记账**：BTC/ETH/COIN 的待入账、确认、转账、提现各自落桶，
  余额视图一次看全，不足时不跨资产抵扣；
- **独立守恒**：混合操作 + 回撤后，逐资产校验
  `Σavailable − Σreorg_debt = 净外部注入`；
- **回退隔离**：某资产充值回撤只动本资产；缺口平账流水带资产标记；
  链上转出事件的扣款、不足平账与回撤都只作用于原资产——回撤只退当初
  实扣的可用、冲销当初的不足缺口，不足额/零余额转出回退后不凭空生钱；
- **幂等**：同键同资产 208 回放（含大小写归一）、同键换资产 409、
  自然键重复投喂 409、重复确认恰好一次；
- **兼容**：不带资产的事件/转账/提现存到 COIN，HTTP 余额顶层字段不变；
- **批量**：一批混多资产确认、回撤 + 确认混批时各自落账；
- **并发**：同一钱包两资产各 50 线程并发提现、8 资产并发确认，
  断言无丢失更新、无负余额、流水唯一；
- **HTTP**：余额 `assets` 视图与 `?asset=` 查询、资产维度冲突 409、
  非法资产 400 `INVALID_ASSET`、流水按资产过滤。

测试断言重点为**资金守恒、余额不为负、错误类型可区分、恰好一次语义**；
服务在关键判定（待入账、确认、回撤、缺口平账、重复回放、冲突拒绝）处通过
`wallet_ledger.*` logger 打印事件流水与依据，可用 `caplog` 或 `-s` 查看。
