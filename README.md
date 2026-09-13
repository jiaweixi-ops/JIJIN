# JIJIN — AI 场外基金公司 V1.3-dev

个人研究与真实前瞻模拟盘系统：Kimi 做研究，Qwen 做结构化与冲突整理，DeepSeek 作为唯一 CIO 形成最终模拟候选，Python 负责数据质量、风控、费用、截止时间、账本、结算与对账，飞书作为主要人工确认入口。

> **仅支持前瞻模拟盘，不连接真钱账户，不实现自动实盘。** `LIVE_TRADING_ENABLED=true` 会被启动校验拒绝。本项目不构成投资建议，不承诺收益。

## V1.3 当前闭环

```text
显式注册的可信 RSS / Atom / JSON Feed
  -> 安全采集 + Collection Run / Document 持久化
  -> 13:00 Multi-source Research Dossier
       - 同基金/同账户/同业务日合并
       - 内容去重 + 来源多样性优先 + 字符/条数预算
  -> Consolidated Research Inbox
  -> Kimi 研究
  -> Qwen 结构化 / Evidence 持久化
  -> DeepSeek CIO
  -> SUGGESTED BUY/SELL 候选单
  -> 13:30 提前截止 / 14:00 常规窗口
  -> DataQualityGate + Python RiskService
  -> PENDING_CONFIRM
  -> 真人确认
  -> SimulationBroker
  -> confirmed NAV / 在途结算 / 账本 / 对账
```

自动化硬边界不变：**采集器只获取显式注册来源；自动采集的单篇原始 Inbox 不能直接进入 AI，必须先进入 Dossier；研究流水线最多生成 `SUGGESTED`；日内编排最多推进到 `PENDING_CONFIRM`。系统不会自动代表用户批准订单。** DeepSeek CIO 不可用时返回 WAIT/BLOCKED，不由 Kimi 或 Qwen 顶替。

## V1.3 已接通

- V1.2.2 安全与账务基线：订单状态机、版本控制、幂等、DataQuality、NAV 估值日、lot/fee、在途资金、对账、凭证生命周期、AI 配额、Feishu 防重放与 callback 幂等、Alembic、容器加固和基础监控。
- `/portfolio/*` 等金融数据 API 均要求内部认证；`/health`、`/ready` 和经过飞书签名验证的事件入口按设计例外。
- `OperationalRun` 以 `(job_name, business_date)` 作为持久化幂等边界，支持失败重试和 stale RUNNING 接管。
- Phase 3 外部采集只访问**显式注册**的 RSS/Atom/JSON Feed；HTTPS 默认强制，拒绝 embedded credentials、localhost、literal private IP，并在实际请求与每次 redirect 前做 public-DNS 校验。
- Collection Source / Run / Document 全部持久化；相同 `external_id + content_sha256` 不重复进入 Research Inbox。
- Phase 4 以 `(account_id, fund_id, business_date)` 形成唯一 `ResearchDossier`，防止同一基金一天因多篇自动采集材料生成多条相互竞争的候选路径。
- Dossier 对 syndicated identical content 做 `content_sha256` 去重；选材优先覆盖不同来源，再按时间补足，受 `MAX_MATERIALS` 与 `MAX_CHARS` 双预算约束。
- 超出 72h 默认 lookback 的自动材料标记为 `STALE`；当日 Dossier 已存在后新到材料标记 `DEFERRED`，可在后续业务日重新进入聚合。
- 被 Dossier 吸收的原始 Research Inbox 标记 `SUPERSEDED`；即使手工调用 process，也会被流水线拒绝直接处理。
- ETag / Last-Modified 支持条件请求；HTTP 304 记成功但不产生新材料。
- 08:15：第一轮可信来源采集。
- 08:45：模拟账户盘前简报、DataQuality 汇总、过期候选清理。
- 12:45：第二轮可信来源采集。
- **13:00：按账户/基金汇总多来源 Dossier。**
- 13:15：仅处理普通人工 Inbox 与 Consolidated Dossier Inbox，转换为审计可追踪的 `SUGGESTED` 候选。
- 13:30：提前截止基金优先进入确定性风控。
- 14:00：其余候选进入确定性风控；通过后停在 `PENDING_CONFIRM` 并发送交易卡。
- 20:30：月末交易日探测与模拟月报；未完成结算时保持 PROVISIONAL。
- Research Inbox 分阶段持久化 `research_packet`、`structured_packet`、`decision_plan` 与 `candidate_order_ids`，暂时失败后可恢复。
- 结构化 evidence 必须绑定到已摄入的 `source_name/source_url`；模型引用 Inbox 之外的来源会被拒绝。
- DataQuality RED 在研究模型调用前 fail-closed。
- DecisionPlan 到订单映射只允许当前 inbox 对应基金的 BUY/SELL，一条 fund-scoped research item 最多产生一个候选，CONVERT 继续拒绝。

## 关键 API

认证与管理：

```text
POST /credentials
POST /credentials/{id}/rotate
POST /credentials/{id}/revoke
GET  /credentials/users/{user_id}
PUT  /data-quality/sources/{name}
POST /data-quality/funds/{fund_id}/rules
GET  /data-quality/funds/{fund_id}
GET  /decisions/quota
```

V1.3 外部研究采集：

```text
POST  /research/collection/sources                     # system / ADMIN
GET   /research/collection/sources
PATCH /research/collection/sources/{source_id}         # system / ADMIN
POST  /research/collection/sources/{source_id}/collect # system / ADMIN
GET   /research/collection/runs
GET   /research/collection/documents
GET   /research/collection/documents/{document_id}
```

V1.3 多来源 Dossier：

```text
GET  /research/dossiers
GET  /research/dossiers/{dossier_id}
POST /research/dossiers/assemble                       # system / ADMIN
```

V1.3 日常运行：

```text
GET  /operations/runs
POST /operations/run/{job_name}

POST /research/inbox
GET  /research/inbox
GET  /research/inbox/{item_id}
GET  /research/inbox/{item_id}/evidence
POST /research/inbox/{item_id}/process
```

所有 `/research/*` 均要求内部认证。Research Inbox 写操作允许 system、EDITOR、ADMIN；collection source 管理、手工抓取与 dossier 手工 assemble 只允许 system 或 ADMIN。

## 可信研究来源边界

Phase 3 **不是开放网络爬虫**。调度器不会接受临时 URL，也不会允许模型自己添加来源。只有先通过 `/research/collection/sources` 注册的 feed 才会被自动访问。

默认网络规则：

```text
HTTPS only
no URL username/password
no localhost / *.localhost
no literal private/loopback/link-local/reserved IP
DNS must resolve only to public/global addresses
redirect target is revalidated
response body/content-type/redirect count are bounded
```

应用层 DNS 预检查并不能理论上完全消除 DNS rebinding；生产容器仍应配置网络 egress policy，禁止访问 RFC1918、云 metadata、宿主机和内部控制面。

`ResearchCollectionSource` 负责研究材料来源；`DataSource` / `DataQualityGate` 负责基金交易规则/NAV/公告健康度。两者职责分离，不能因为 feed 抓取成功就自动认为基金 DataQuality 是 GREEN。

详细规则见 `docs/V1.3_RESEARCH_COLLECTION.md`。

## Multi-source Research Dossier

自动采集材料与人工 Research Inbox 的语义不同。人工主动提交的 Inbox 可以直接进入 13:15 流水线；自动采集产生的单篇原始 Inbox 只是**审计留痕载体**，不允许直接跑模型。

13:00 Dossier 阶段对每个 `(account_id, fund_id)`：

1. 只考虑默认 72 小时 lookback 内、尚未归档的自动采集文档。
2. 先按 `content_sha256` 去掉不同 feed 转载的相同正文。
3. 先取每个来源最新一条，保证来源覆盖，再按新鲜度填剩余槽位。
4. 默认最多 12 条、总正文 80,000 字符，且不会突破全局 `AI_MAX_SOURCE_CHARS`。
5. 生成一天唯一的 Consolidated Research Inbox。
6. 原单篇 Inbox 变为 `SUPERSEDED`，防止重复 AI 调用和重复候选。

如果 13:00 后又抓到新材料，当日已有 Dossier 不会被静默改写；新材料进入 `DEFERRED`，后续业务日按 lookback 重新考虑。这样 DecisionPlan 的输入集合在同一业务日是可审计、可重放的固定快照。

详细规则见 `docs/V1.3_RESEARCH_DOSSIERS.md`。

## Research Inbox 的幂等边界

Inbox 幂等边界：

```text
(account_id, idempotency_key)
```

同时保存整个规范化请求的 `payload_hash`。同 key 同 payload 返回原记录；同 key 不同 payload 拒绝，防止“重试”静默改写历史。采集器生成的 idempotency key 由 document fingerprint 确定；Dossier 的 idempotency key 由账户、基金、业务日和选中文档 fingerprint 集合确定。

详细状态、恢复和来源校验规则见 `docs/V1.3_RESEARCH_PIPELINE.md`。

## DataQuality 与结算

`research_quality` 和 `settlement_eligibility` 始终分开：

- RED：研究交易候选自动化被阻断。
- YELLOW：可以做有限研究/估算，但不等于可结算。
- 正式模拟成交只使用与订单 `valuation_date` 精确匹配的 confirmed NAV。

每只参与自动研究/候选的基金都必须先注册启用的数据源，并摄入基金规则快照；否则 RED 是预期的 fail-closed 行为。

## AI 配额与成本

主要环境变量：

```text
AI_MAX_SOURCE_CHARS
AI_MAX_REQUEST_CHARS
AI_MAX_OUTPUT_TOKENS
AI_DAILY_MAX_CALLS_PER_SUBJECT
AI_DAILY_MAX_ESTIMATED_COST
DEEPSEEK_*_COST_PER_MILLION
QWEN_*_COST_PER_MILLION
KIMI_*_COST_PER_MILLION
```

价格为 0 表示未知价格；字符/token/call-count 限制仍生效。配置价格后，调用前使用偏保守 token 上界：ASCII/窄字符按 1 token/字符，CJK/东亚宽字符按 2 token/字符，并预留最大输出 token。provider 不返回 usage 时，调用后也使用保守上界记入 `estimated_cost`。

V1.3 日配额仍是个人工具场景的 best-effort “查累计 -> 判断 -> 调用 -> 记账”；多 worker 严格预算需要原子日预算预留行/行锁。

## V1.3 调度

默认 `Asia/Shanghai`：

```text
08:15  research_collection_morning
08:45  morning_brief
12:45  research_collection_predecision
13:00  research_dossiers
13:15  research_pipeline
13:30  early_cutoff
14:00  decision_window
20:30  month_end_probe
08:00-22:00 / 15min  settle_due_cash
```

APScheduler 只负责触发；交易日、业务日、幂等和订单边界都在业务层重新校验。Dossier 失败时，13:15 流水线仍会排除原始自动采集 Inbox，因此失败方向是“少做一次研究”，而不是“同一基金按多篇文章各做一次决策”。

Research 配置：

```text
RESEARCH_PROCESSING_STALE_MINUTES=30
RESEARCH_PIPELINE_BATCH_SIZE=5
RESEARCH_PIPELINE_MAX_ATTEMPTS=5
RESEARCH_COLLECTION_TIMEOUT_SECONDS=15
RESEARCH_COLLECTION_MAX_BYTES=2000000
RESEARCH_COLLECTION_MAX_REDIRECTS=3
RESEARCH_COLLECTION_MAX_ITEMS_PER_SOURCE=20
RESEARCH_COLLECTION_BATCH_SIZE=20
RESEARCH_COLLECTION_ALLOW_HTTP=false
RESEARCH_DOSSIER_LOOKBACK_HOURS=72
RESEARCH_DOSSIER_MAX_MATERIALS=12
RESEARCH_DOSSIER_MAX_CHARS=80000
```

## 数据库迁移

Schema 只通过 Alembic 演进：

```text
20260912_01  V1.2.2 baseline / feishu_callback.status_code
20260912_02  ApiCredential lifecycle + AI usage ledger
20260913_01  OperationalRun
20260913_02  Research Inbox + Research Evidence
20260913_03  Research Collection Source + Run + Document
20260913_04  Research Dossier + document dossier binding
```

启动应用前：

```bash
alembic upgrade head
```

`/ready` 同时检查数据库连通性与 Alembic revision 是否等于代码 head；不一致返回 503。

## 快速开始

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
alembic upgrade head
python scripts/seed_demo.py
uvicorn app.main:app --reload
ruff check .
pytest
```

生产/准生产必须配置强随机且彼此不同的 `INTERNAL_API_TOKEN` 和 `API_CREDENTIAL_PEPPER`。若启用飞书，还必须完整配置飞书验签参数。

Docker Compose：

```bash
cp .env.compose.example .env
# 填写 secret 与 DATABASE_URL
docker compose up --build -d
```

API 默认只绑定 `127.0.0.1`；PostgreSQL 不发布宿主机端口；容器以非 root 用户运行。远程访问请置于受控 TLS/VPN/反向代理之后。外部 research collection 最好再叠加容器/主机 egress 防火墙。

## 明确未支持

- 真钱下单与自动实盘。
- 基金转换 CONVERT。
- 自动替代人工批准。
- DeepSeek CIO 故障时的其他 CIO fallback。
- 通用网页爬虫、JavaScript 浏览器抓取、搜索引擎全网发现。
- 模型自行新增/修改采集来源。
- 自动全网抓取并无审查地直接进入交易研究流水线。
- 独立“回测中心”。历史数据仅用于确定性指标、规则验证、事件复盘与异常演练。

## 文档

- `docs/ARCHITECTURE_V1.2.md`：V1.2 基础架构与安全账务原则。
- `docs/OPERATIONS_V1.2.2.md`：凭证、AI 配额、DataQuality、Docker/监控 hardening。
- `docs/V1.3_OPERATIONAL_SIMULATION.md`：Phase 1 每日运行编排。
- `docs/V1.3_RESEARCH_PIPELINE.md`：Phase 2 可信研究材料到候选单。
- `docs/V1.3_RESEARCH_COLLECTION.md`：Phase 3 可信外部 feed 采集与网络安全边界。
- `docs/V1.3_RESEARCH_DOSSIERS.md`：Phase 4 多来源基金/业务日研究档案与候选降噪。
- `docs/DB_MIGRATIONS_V1.2.2.md`：数据库迁移运行手册（迁移命令继续适用于 V1.3）。
