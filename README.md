# JIJIN — AI 场外基金公司 V1.2.2

个人研究与真实前瞻模拟盘系统：Kimi 做研究、Qwen 做结构化、DeepSeek 做 CIO 推理、Python 做账本/风控/费用/截止时间/对账，飞书作为主要交互入口。

> V1.2.2 **仅支持 SimulationBroker 模拟执行**，不连接任何真钱账户、不包含基金销售平台真实下单接口；不构成投资建议，不承诺收益。`LIVE_TRADING_ENABLED=true` 会被启动校验直接拒绝。

## V1.2.2 已接通

- 订单状态机、条件更新版本控制、幂等键与旧卡失效
- 场外基金逐基金截止时间；内部按 UTC 保存语义，飞书展示按配置时区转换
- 正数金额/份额/比例校验，费用版本、FIFO lot allocation、短持有风险与紧急退出双人审批
- 可用/冻结/在途资金与确认份额账本；赎回 T+N 在途现金由调度任务到期结转
- 对账 run / diff / resolve API；未解决 blocking diff 阻断交易，解决后恢复
- 数据质量双维度：`research_quality` 与 `settlement_eligibility`
- `SimulationBroker` 只使用与订单 `valuation_date` 匹配的已确认 `NavConfirm`，不接受调用方传入成交净值
- Kimi → Qwen → DeepSeek 的结构化 AI Gateway 已通过受认证 `/decisions/run` 接入
- 飞书签名校验、防重放/回调幂等、交易卡片发送、会话绑定、自然语言修改及版本检查
- 内部 API 使用服务凭证；人工审批必须使用绑定真实 `User` 的独立 `ApiCredential`，请求头不能自由伪造 actor
- `ApiCredential` 已有完整生命周期：按用户签发、HMAC-SHA256 + server pepper、到期、吊销、轮换、`last_used_at` 和审计日志；旧 SHA-256 凭证仅作为迁移兼容，建议轮换
- AI 配额按主体执行：源材料/单次请求/输出 token 上限、每日调用次数、每日估算成本上限；`AIUsageLedger` 与 `ModelCallLog.estimated_cost` 留痕，`GET /decisions/quota` 可查看当前额度
- DataQualityGate 已接真实持久化数据源：`DataSource` 优先级/SLA、基金申赎与限购快照、费率版本、公告抓取、关键字段缺失和多源冲突；RED 会阻断研究交易候选
- Alembic schema versioning；应用启动只校验数据库 revision，不再自动 `create_all`
- Ruff 已进入 CI；SQLite / PostgreSQL migration 都有 CI 验证
- Docker 以非 root 用户运行，带 `/ready` HEALTHCHECK；Compose 默认只绑定 `127.0.0.1`
- Compose 不再内置弱口令，数据库和 API 都有 restart / 最小权限配置
- HTTP 请求日志包含 request_id、route、status、duration；`/metrics/` 提供 Prometheus 基础指标
- pytest 回归测试与 GitHub Actions

## 关键管理入口

- `POST /credentials`：签发用户凭证，明文 token 只返回一次
- `POST /credentials/{id}/rotate`：轮换并立即吊销旧凭证
- `POST /credentials/{id}/revoke`：人工吊销
- `GET /credentials/users/{user_id}`：查看不含 token/hash 的凭证生命周期信息
- `PUT /data-quality/sources/{name}`：注册/更新可信数据源优先级、SLA 和启停状态
- `POST /data-quality/funds/{fund_id}/rules`：写入基金规则/公告观测快照
- `GET /data-quality/funds/{fund_id}`：查看当前 research quality 与 settlement eligibility
- `GET /decisions/quota`：查看当前认证主体的当日 AI 调用/成本配额

以上管理入口都需要内部认证；凭证和数据源管理仅允许 system principal 或 ADMIN。

## 明确未支持

- **CONVERT 基金转换：V1.2.2 直接拒绝创建**，避免未完成双腿结算时冻结份额；计划在后续版本单独实现。
- 自动实盘交易。
- 08:45 盘前简报、13:30 提醒、14:00 决策卡、20:30 月末任务目前仍只有调度入口；其中赎回在途现金结算已接入真实业务逻辑，其余业务编排继续迭代。

## 时间与数据规则

- 领域逻辑使用 timezone-aware datetime；数据库读取到的 naive datetime 在本项目中统一解释为 **UTC**。
- 截止时间/卡片展示转换为 `TIMEZONE`（默认 `Asia/Shanghai`）。
- 未确认 NAV 可以作为研究估算，因此 `research_quality` 可能为 YELLOW；但正式模拟结算必须 `settlement_eligibility=true`，并且使用与该订单估值日匹配的 confirmed NAV。
- 基金申赎/限购/费率/公告状态不再默认“已知”：必须来自已启用 `DataSource` 的持久化规则快照；超过该数据源 SLA、关键字段缺失、费率变化未确认或关键多源冲突都会进入 RED。
- 多源冲突时低数字 `priority` 代表更高权威。低优先级冲突快照不会覆盖基金有效规则，但冲突本身仍会使质量 RED，直到权威数据重新确认。
- 非交易日提交的模拟候选可解析到下一开放交易日估值；这不代表已经成交。

## AI 配额与成本

AI 预算由环境变量控制：

- `AI_MAX_SOURCE_CHARS`
- `AI_MAX_REQUEST_CHARS`
- `AI_MAX_OUTPUT_TOKENS`
- `AI_DAILY_MAX_CALLS_PER_SUBJECT`
- `AI_DAILY_MAX_ESTIMATED_COST`
- 三家模型各自的 input/output 每百万 token 价格

价格单位由 `AI_COST_CURRENCY` 指定。价格保持 `0` 表示当前没有配置可靠价格，此时仍执行字符/token/call-count 配额，但金额成本为 0。

配置价格后，Gateway 的调用前预算使用**故意偏高的上界**：ASCII/窄字符按 1 token/字符预留，CJK/东亚宽字符按 2 token/字符预留，并始终预留 `AI_MAX_OUTPUT_TOKENS`。这不是 tokenizer 的精确估算，而是成本闸门的 fail-closed 上界。

模型返回 `usage.prompt_tokens` / `usage.completion_tokens` 时，调用后成本按 provider usage 记账；任一字段缺失、非法或为负时，对缺失部分改用上述保守输入 token 上界或 `AI_MAX_OUTPUT_TOKENS` 兜底，因此不会再因为 provider 不返回 usage 而把该次成本记成 0。为保留“provider 是否真的回了 token”的语义，`AIUsageLedger.input_tokens/output_tokens` 仍保存 provider 原值；缺失时可以为 NULL，但 `estimated_cost` 仍会是保守非零估算。

V1.2.2 的每日调用/成本闸门仍是“查询当日累计 → 判断 → 调用 → 写入 usage”的 best-effort 方案，不是严格串行的预算预留器。个人单操作者/低并发场景可接受；多 worker 并发时可能出现小幅超限。若以后要求严格额度，应改为按主体+UTC 日的原子计数/预算预留行，并用条件更新或数据库行锁实现。

## 数据库迁移

数据库 schema 从 V1.2.2 开始由 Alembic 管理。无论 SQLite 开发环境还是 PostgreSQL 目标环境，启动应用前都必须先执行：

```bash
alembic upgrade head
```

迁移链：

- `20260912_01`：接管 V1.2.1 / 建立 V1.2.2 baseline，并补 `feishu_callback.status_code`
- `20260912_02`：增加 ApiCredential 生命周期字段和 `ai_usage_ledger`

应用启动时检查 `alembic_version == code head`，版本不匹配会 fail-fast。以后所有 schema 变化必须新增 Alembic revision，不能依赖 `Base.metadata.create_all()` 升级生产数据库。

生产数据库执行 migration 前应先做可恢复备份。详细迁移 runbook：`docs/DB_MIGRATIONS_V1.2.2.md`。

## Docker Compose

```bash
cp .env.compose.example .env
# 填写 INTERNAL_API_TOKEN / API_CREDENTIAL_PEPPER / POSTGRES_PASSWORD / DATABASE_URL

docker compose up --build -d
```

Compose 会先等待 PostgreSQL healthy，再运行一次 `alembic upgrade head`，migration 成功后才启动 API。API 默认仅绑定 `127.0.0.1:8000`。远程访问应放在受控反向代理、VPN 或 TLS 网关之后。

健康与监控：

- `/health`：进程 liveness
- `/ready`：数据库 + Alembic readiness
- `/metrics/`：Prometheus 基础 HTTP 指标
- 响应头 `X-Request-ID`：请求追踪

详细运行说明见 `docs/OPERATIONS_V1.2.2.md`。

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

> **V1.2.2 数据质量启动要求：** `seed_demo.py` 只为演示基金自动造可信数据源、规则快照和 confirmed NAV。接入任何真实基金时，必须先通过 `PUT /data-quality/sources/{name}` 注册/启用数据源，再通过 `POST /data-quality/funds/{fund_id}/rules` 摄入最新规则快照，并提供可用 NAV；否则 `DataQualityGate` 会按设计返回 RED，研究交易候选/新订单会被阻断。这不是升级故障。

生产/准生产环境必须配置 `INTERNAL_API_TOKEN`。若需要签发人工凭证，还必须配置独立的强随机 `API_CREDENTIAL_PEPPER`；该 pepper 不下发给客户端。若启用飞书，还必须完整配置飞书验签参数。

详细开发基线见 `docs/ARCHITECTURE_V1.2.md`。
