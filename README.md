# JIJIN — AI 场外基金公司 V1.3-dev

个人研究与真实前瞻模拟盘系统：Kimi 做研究，Qwen 做结构化与冲突整理，DeepSeek 作为唯一 CIO 形成最终模拟候选，Python 负责数据质量、风控、费用、截止时间、账本、结算与对账，飞书作为主要人工确认入口。

> **仅支持前瞻模拟盘，不连接真钱账户，不实现自动实盘。** `LIVE_TRADING_ENABLED=true` 会被启动校验拒绝。本项目不构成投资建议，不承诺收益。

## V1.3 当前闭环

```text
可信研究材料
  -> Research Inbox（持久化 + payload hash + 幂等）
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

自动化的硬边界不变：**研究流水线最多生成 `SUGGESTED`；日内编排最多推进到 `PENDING_CONFIRM`。系统不会自动代表用户批准订单。** DeepSeek CIO 不可用时返回 WAIT/BLOCKED，不由 Kimi 或 Qwen 顶替。

## V1.3 已接通

- V1.2.2 全部安全与账务基线：订单状态机、版本控制、幂等、DataQuality、NAV 估值日、lot/fee、在途资金、对账、凭证生命周期、AI 配额、Feishu 防重放与 callback 幂等、Alembic、容器加固和基础监控。
- `/portfolio/*` 等金融数据 API 均要求内部认证；`/health`、`/ready` 和经过飞书签名验证的事件入口按设计例外。
- `OperationalRun` 以 `(job_name, business_date)` 作为持久化幂等边界，支持失败重试和 stale RUNNING 接管。
- 08:45：模拟账户盘前简报、DataQuality 汇总、过期候选清理。
- 13:15：处理可信 Research Inbox，将研究材料转换为审计可追踪的 `SUGGESTED` 候选。
- 13:30：提前截止基金优先进入确定性风控。
- 14:00：其余候选进入确定性风控；通过后停在 `PENDING_CONFIRM` 并发送交易卡。
- 20:30：月末交易日探测与模拟月报；未完成结算时保持 PROVISIONAL。
- Research Inbox 分阶段持久化 `research_packet`、`structured_packet`、`decision_plan` 与 `candidate_order_ids`，暂时失败后可恢复，不必无意义重复所有模型调用。
- 结构化 evidence 必须绑定到已摄入的 `source_name/source_url`；模型引用未进入 Inbox 的来源会被拒绝。
- DataQuality RED 在研究模型调用前 fail-closed；不会消耗 Kimi/Qwen/DeepSeek 调用后再发现不能交易。
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

所有 `/research/*` 均要求内部认证；研究写操作仅 system、EDITOR 或 ADMIN。Research 列表不返回完整材料正文，详情才返回。

## Research Inbox 的可信边界

`POST /research/inbox` 必须由受认证主体主动摄入材料。当前 Phase 2 **没有自动全网抓取器**。每份材料记录来源、时间戳、正文和 `content_sha256`。

Inbox 幂等边界为：

```text
(account_id, idempotency_key)
```

同时保存整个规范化请求的 `payload_hash`。同 key 同 payload 返回原记录；同 key 不同 payload 拒绝，防止“重试”变成静默改写历史。

详细状态、恢复和来源校验规则见 `docs/V1.3_RESEARCH_PIPELINE.md`。

## DataQuality 与结算

`research_quality` 和 `settlement_eligibility` 始终分开：

- RED：研究交易候选自动化被阻断。
- YELLOW：可以做有限研究/估算，但不等于可结算。
- 正式模拟成交只使用与订单 `valuation_date` 精确匹配的 confirmed NAV。

每只参与自动研究/候选的基金都必须先注册启用的数据源，并摄入基金规则快照；否则 RED 是预期的 fail-closed 行为，不是系统故障。

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

价格为 0 表示未知价格；字符/token/call-count 限制仍生效。配置价格后，调用前使用偏保守的 token 上界：ASCII/窄字符按 1 token/字符，CJK/东亚宽字符按 2 token/字符，并预留最大输出 token。provider 不返回 usage 时，调用后也使用保守上界记入 `estimated_cost`，不会把成本错误记为 0。

V1.3 的日配额仍是个人工具场景的 best-effort “查累计 -> 判断 -> 调用 -> 记账”；多 worker 严格预算需要原子日预算预留行/行锁，当前不宣称解决了该竞态。

## V1.3 调度

默认 `Asia/Shanghai`：

```text
08:45  morning_brief
13:15  research_pipeline
13:30  early_cutoff
14:00  decision_window
20:30  month_end_probe
08:00-22:00 / 15min  settle_due_cash
```

APScheduler 只负责触发；交易日、业务日、幂等和订单边界都在业务层重新校验。

Research 配置：

```text
RESEARCH_PROCESSING_STALE_MINUTES=30
RESEARCH_PIPELINE_BATCH_SIZE=5
RESEARCH_PIPELINE_MAX_ATTEMPTS=5
```

## 数据库迁移

Schema 只通过 Alembic 演进：

```text
20260912_01  V1.2.2 baseline / feishu_callback.status_code
20260912_02  ApiCredential lifecycle + AI usage ledger
20260913_01  OperationalRun
20260913_02  Research Inbox + Research Evidence
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

API 默认只绑定 `127.0.0.1`；PostgreSQL 不发布宿主机端口；容器以非 root 用户运行。远程访问请置于受控 TLS/VPN/反向代理之后。

## 明确未支持

- 真钱下单与自动实盘。
- 基金转换 CONVERT。
- 自动替代人工批准。
- DeepSeek CIO 故障时的其他 CIO fallback。
- 自动全网抓取并无审查地直接进入交易研究流水线。
- 独立“回测中心”。历史数据仅用于确定性指标、规则验证、事件复盘与异常演练。

## 文档

- `docs/ARCHITECTURE_V1.2.md`：V1.2 基础架构与安全账务原则。
- `docs/OPERATIONS_V1.2.2.md`：凭证、AI 配额、DataQuality、Docker/监控 hardening。
- `docs/V1.3_OPERATIONAL_SIMULATION.md`：Phase 1 每日运行编排。
- `docs/V1.3_RESEARCH_PIPELINE.md`：Phase 2 可信研究材料到候选单。
- `docs/DB_MIGRATIONS_V1.2.2.md`：数据库迁移运行手册（迁移命令继续适用于 V1.3）。
