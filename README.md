# JIJIN — AI 场外基金公司 V1.3.0-rc1

个人研究与真实前瞻模拟盘系统：Kimi 做研究，Qwen 做结构化与冲突整理，DeepSeek 作为唯一 CIO 形成最终模拟候选，Python 负责数据质量、风控、费用、截止时间、账本、结算与对账，飞书作为主要人工确认入口。

> **仅支持场外基金前瞻模拟盘，不连接真钱账户，不实现自动实盘。** `LIVE_TRADING_ENABLED=true` 会被启动校验拒绝。本项目不构成投资建议，不承诺收益。

当前版本是 **`1.3.0rc1`**。工程资格已经完成；稳定版 `1.3.0` 只有在真实前瞻运行满足 **>=30 个自然日 + 每个启用模拟账户 >=20 个正式交易日 PortfolioSnapshot**，并且发布阻断项全部清零后才允许晋级。CI 中的合成 30/20 数据只验证闸门逻辑，不计入真实观察期。

## 当前闭环

```text
显式注册的可信 RSS / Atom / JSON Feed
  -> 安全采集 + Collection Run / Document 持久化
  -> Multi-source Research Dossier
  -> Consolidated Research Inbox
  -> Kimi 研究
  -> Qwen 结构化 / Evidence 持久化
  -> DeepSeek CIO
  -> SUGGESTED BUY/SELL 候选
  -> DataQualityGate + Python RiskService
  -> PENDING_CONFIRM
  -> 真人确认
  -> SimulationBroker
  -> confirmed NAV / 在途结算 / 账本 / 对账
  -> PortfolioSnapshot
  -> 前瞻 DecisionReview / AI 角色评价 / 管理报告
  -> Release Qualification / RC Field Observation
```

自动化硬边界：**采集器只访问显式注册来源；自动采集的单篇原始 Inbox 不能直接进入 AI，必须先形成 Dossier；研究流水线最多生成 `SUGGESTED`；日内编排最多推进到 `PENDING_CONFIRM`；系统不会自动代表用户批准或提交订单。** DeepSeek CIO 不可用时 WAIT/BLOCKED，不由 Kimi 或 Qwen 顶替。

## V1.3 已接通

- V1.2.2 安全与账务基线：订单状态机、版本控制、幂等、DataQuality、NAV 估值日、lot/fee、在途资金、对账、凭证生命周期、AI 配额、Feishu 防重放与 callback 幂等、Alembic、容器加固、日志和 Prometheus。
- `/portfolio/*` 等金融数据 API 均要求内部认证；`/health`、`/ready` 和经过飞书签名验证的事件入口按设计例外。
- `OperationalRun` 以 `(job_name, business_date)` 作为持久化幂等边界，支持失败重试和 stale RUNNING 接管。
- 外部研究采集仅访问显式注册的 RSS/Atom/JSON Feed；HTTPS 默认强制，并做 URL/DNS/redirect/响应大小等 SSRF 防护。
- `ResearchDossier` 以 `(account_id, fund_id, business_date)` 唯一聚合，多来源正文去重、来源多样性优先，并受条数/字符双预算限制。
- 自动采集单篇原始 Inbox 被 Dossier 吸收后进入 `SUPERSEDED`，不能绕过 Dossier 单独调用模型。
- Research Inbox 分阶段持久化 `research_packet`、`structured_packet`、`decision_plan`、`candidate_order_ids`；结构化 evidence 必须绑定已摄入来源。
- DecisionPlan 只允许当前基金 BUY/SELL，一条 fund-scoped research item 最多产生一个候选；CONVERT 继续拒绝。
- 真实基金数据通过显式注册的 `FundDataConnector` 接入，保留原始 observation、同步 run、ETag/Last-Modified、来源优先级与冲突审计。
- PENDING/ESTIMATED/PROVISIONAL NAV 永远不能创建正式 `NavConfirm`；低权威 confirmed NAV 不能覆盖更高权威数据；confirmed 冲突会使 DataQuality fail-closed。
- Portfolio Risk 已纳入待确认 BUY 软预留现金、待确认 SELL lot 软预留、单基金权重、单日交易比例、组合回撤和连续亏损；减仓 SELL 不因组合已回撤而被锁死。
- `OperationalAlert` 支持 OPEN / ACKNOWLEDGED / RESOLVED / reopen，带去重、防告警风暴和飞书通知。
- `DecisionReview` 以 5/20/60 天等前瞻周期评价当时决策；AIContributionScore 分角色评价 Kimi/Qwen/DeepSeek；不会自动修改策略、Prompt、风险参数或模型路由。
- 周/月 `ManagementReport` 聚合正式 PortfolioSnapshot、订单、风险拒绝、告警、对账和 AI 成本。
- `ReleaseQualificationRun` 提供 ENGINEERING_READY / FIELD_OBSERVATION_PENDING / RELEASE_READY 发布资格链；无人工强制 RELEASE_READY 开关。
- RC Field Operations 提供部署 preflight、30/20 观察进度、每日 job、告警和最新正式快照可见性。

## 关键 API

认证与 DataQuality：

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

真实基金数据：

```text
POST  /fund-data/connectors
GET   /fund-data/connectors
PATCH /fund-data/connectors/{connector_id}
POST  /fund-data/connectors/{connector_id}/sync
GET   /fund-data/runs
GET   /fund-data/observations
```

研究采集与 Dossier：

```text
POST  /research/collection/sources
GET   /research/collection/sources
PATCH /research/collection/sources/{source_id}
POST  /research/collection/sources/{source_id}/collect
GET   /research/collection/runs
GET   /research/collection/documents
GET   /research/dossiers
GET   /research/dossiers/{dossier_id}
POST  /research/dossiers/assemble
```

Research Inbox / 日常运行：

```text
POST /research/inbox
GET  /research/inbox
GET  /research/inbox/{item_id}
GET  /research/inbox/{item_id}/evidence
POST /research/inbox/{item_id}/process

GET  /operations/runs
POST /operations/run/{job_name}
GET  /operations/alerts
POST /operations/alerts/sweep
POST /operations/alerts/{alert_id}/ack
```

复盘、管理报告与发布资格：

```text
GET  /reviews/decisions
POST /reviews/sync
POST /reviews/resolve
GET  /reviews/ai-performance
GET  /management/reports
POST /management/reports/generate

GET  /qualification/status
GET  /qualification/runs
POST /qualification/engineering
POST /qualification/field-gate

GET  /field-operations/status
```

所有金融、研究、运行、复盘、资格和 Field Operations 数据接口都需要内部认证；管理/手工执行类写操作进一步限制到 system 或 ADMIN，告警 ACK 要求真实 ADMIN 用户主体。

## DataQuality 与 NAV 结算

`research_quality` 与 `settlement_eligibility` 始终分开：

- RED：研究交易候选自动化被阻断。
- YELLOW：可以做有限研究/估算，但不等于可结算。
- 正式模拟成交只使用与订单 `valuation_date` 精确匹配的 confirmed NAV。

每只参与自动研究/候选的基金都必须先注册启用的数据源并摄入可信基金规则；否则 RED 是预期的 fail-closed 行为。研究 feed 抓取成功不会自动把基金 DataQuality 变成 GREEN。

## 可信外部来源边界

V1.3 **不是开放网络爬虫**。模型不能自行添加 URL；只有已注册 source/connector 才能被自动访问。默认网络边界：

```text
HTTPS only
no embedded URL credentials
no localhost / *.localhost
no private / loopback / link-local / reserved literal IP
DNS must resolve only to public/global addresses
redirect target is revalidated
response body / content type / redirect count are bounded
```

应用层 DNS 校验不能理论上完全消除 DNS rebinding；正式运行环境仍建议通过容器/主机 egress policy 阻止 RFC1918、云 metadata、宿主机和内部控制面。

## AI 配额与成本

AI Gateway 有请求字符、输出 token、主体每日调用数和估算成本边界。调用前预算使用偏保守上界：ASCII/窄字符按 1 token/字符，CJK/东亚宽字符按 2 token/字符，并预留完整最大输出。provider 未返回有效 usage 时，成本记账也使用保守兜底；原始 token 字段仍可保持 NULL，避免把估算伪装成 provider 实际值。

V1.3 日配额仍是个人低并发场景的 best-effort `查累计 -> 判断 -> 调用 -> 记账`；它不是并发严格原子预算。多 worker 场景要升级为独立日预算预留行/行锁。

## V1.3 默认调度（Asia/Shanghai）

```text
07:45  fund_data_premarket
08:15  research_collection_morning
08:45  morning_brief
12:45  research_collection_predecision
13:00  research_dossiers
13:10  fund_data_predecision
13:15  research_pipeline
13:30  early_cutoff
14:00  decision_window
18:00  fund_data_postclose
20:30  month_end_probe
22:15  fund_data_presnapshot
22:30  portfolio_snapshot
23:15  fund_data_late
23:30  portfolio_snapshot_retry
23:35  decision_reviews
23:50  management_reports
23:55  release_qualification
08:00-22:00 / 15min  settle_due_cash
08:00-23:30 / 30min  operational_alerts
```

`fund_data_predecision` 明确位于 13:15 Research Pipeline **之前**，避免 DataQualityGate 在研究模型调用前仍依赖 07:45 的规则/NAV 观察。APScheduler 只负责触发；交易日、业务日、幂等、DataQuality、订单状态和人工确认边界都由业务层重新校验。

## RC Field Observation

V1.3 `1.3.0rc1` 的正式观察环境要求 PostgreSQL。运行前：

```bash
python scripts/field_operations.py preflight
```

部署 preflight 会 fail-closed 检查：prod/staging 环境、PostgreSQL、ENGINEERING_READY、至少一个启用模拟账户、至少一个启用 FundDataConnector、至少一个启用研究来源，以及 DeepSeek/Qwen/Kimi key+model 是否齐全。Feishu 未开启会作为覆盖不足 warning，而不是伪装成已完成的人机链路。

正式开始观察期：

```bash
python scripts/release_qualification.py engineering
```

必须得到 `ENGINEERING_READY` 且 `blocker_count=0`。随后真实运行，直到服务器端资格闸门同时满足：

```text
>= 30 real natural days
AND every enabled simulation account >= 20 formal CN business-day PortfolioSnapshot dates
AND no release-blocking accounting/data-integrity alert
AND no unresolved blocking reconciliation diff
AND no FAILED operational run in the observation window
AND no matured unresolved DecisionReview
AND LIVE_TRADING_ENABLED=false
```

查看当前状态：

```bash
python scripts/field_operations.py status
python scripts/release_qualification.py field
```

只有 `/qualification/status` 返回 `field.status=RELEASE_READY`、`stable_release_ready=true` 后，才允许把 `1.3.0rc1` 晋级为稳定版 `1.3.0`。

详细部署、真实数据初始化、每日检查和 PostgreSQL backup/restore drill 见 `docs/V1.3_FIELD_OPERATIONS.md`。

## 数据库迁移

Schema 只通过 Alembic 演进：

```text
20260912_01  V1.2.2 baseline / feishu_callback.status_code
20260912_02  ApiCredential lifecycle + AI usage ledger
20260913_01  OperationalRun
20260913_02  Research Inbox + Research Evidence
20260913_03  Research Collection Source + Run + Document
20260913_04  Research Dossier + document dossier binding
20260913_05  OperationalAlert
20260913_06  FundDataConnector + SyncRun + Observation
20260913_07  DecisionReview + AIContributionScore + ManagementReport
20260914_01  ReleaseQualificationRun
```

启动应用前必须：

```bash
alembic upgrade head
```

`/ready` 会同时检查数据库连通性和 Alembic revision；不一致返回 503。

## 快速开始

开发/测试：

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

RC / 长期运行推荐 Docker Compose + PostgreSQL：

```bash
cp .env.compose.example .env
# 填写数据库、独立强随机认证 secret、三家 AI key/model、真实数据源和飞书配置
docker compose up --build -d
python scripts/field_operations.py preflight
```

API 默认只绑定 `127.0.0.1`；PostgreSQL 不发布宿主机端口；容器以非 root 用户运行。远程访问请置于受控 TLS/VPN/反向代理之后。

## 明确未支持

- 真钱下单与自动实盘。
- 基金转换 CONVERT。
- 自动替代人工批准。
- DeepSeek CIO 故障时的其他 CIO fallback。
- 通用网页爬虫、JavaScript 浏览器抓取、搜索引擎全网发现。
- 模型自行新增/修改采集来源。
- 独立“回测中心”。历史数据只用于确定性指标、规则验证、事件复盘和异常演练。
- 自动依据短期收益改 Prompt、策略、风险参数或模型路由。

## 文档

- `docs/ARCHITECTURE_V1.2.md`：基础架构与安全账务原则。
- `docs/OPERATIONS_V1.2.2.md`：凭证、AI 配额、DataQuality、Docker/监控 hardening。
- `docs/V1.3_OPERATIONAL_SIMULATION.md`：每日运行编排。
- `docs/V1.3_RESEARCH_PIPELINE.md`：可信研究材料到候选单。
- `docs/V1.3_RESEARCH_COLLECTION.md`：可信外部 feed 采集与网络安全边界。
- `docs/V1.3_RESEARCH_DOSSIERS.md`：多来源基金/业务日研究档案。
- `docs/V1.3_RELEASE_QUALIFICATION.md`：工程资格与 30/20 真实观察闸门。
- `docs/V1.3_FIELD_OPERATIONS.md`：RC 部署、preflight、日常运行、backup/restore 与稳定版晋级。
- `docs/DB_MIGRATIONS_V1.2.2.md`：数据库迁移运行手册（迁移命令继续适用于 V1.3）。
