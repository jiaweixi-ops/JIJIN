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
- Alembic schema versioning；应用启动只校验数据库 revision，不再自动 `create_all`
- pytest 回归测试与 GitHub Actions

## 明确未支持

- **CONVERT 基金转换：V1.2.2 直接拒绝创建**，避免未完成双腿结算时冻结份额；计划在后续版本单独实现。
- 自动实盘交易。
- 08:45 盘前简报、13:30 提醒、14:00 决策卡、20:30 月末任务目前仍只有调度入口；其中赎回在途现金结算已接入真实业务逻辑，其余业务编排继续迭代。
- 完整 Docker 加固等工程化工作仍后置。

## 时间与数据规则

- 领域逻辑使用 timezone-aware datetime；数据库读取到的 naive datetime 在本项目中统一解释为 **UTC**。
- 截止时间/卡片展示转换为 `TIMEZONE`（默认 `Asia/Shanghai`）。
- 未确认 NAV 可以作为研究估算，因此 `research_quality` 可能为 YELLOW；但正式模拟结算必须 `settlement_eligibility=true`，并且使用与该订单估值日匹配的 confirmed NAV。
- 非交易日提交的模拟候选可解析到下一开放交易日估值；这不代表已经成交。

## 数据库迁移

数据库 schema 从 V1.2.2 开始由 Alembic 管理。无论 SQLite 开发环境还是 PostgreSQL 目标环境，启动应用前都必须先执行：

```bash
alembic upgrade head
```

首个 revision `20260912_01` 是迁移接管基线：

- **新数据库**：创建冻结的 V1.2.2 baseline schema。
- **旧 V1.2.1 数据库**：保留已有表和数据，创建缺失的 baseline 表，并补 `feishu_callback.status_code`。
- 应用启动时只检查 `alembic_version` 是否等于代码的 Alembic head；版本不匹配会 fail-fast，并提示先运行 `alembic upgrade head`。
- 以后所有 schema 变化必须新增 Alembic revision，不能再依赖应用启动时 `Base.metadata.create_all()` 升级数据库。

生产数据库执行 migration 前仍应先做可恢复备份。首个接管 revision 的 downgrade 只撤销 V1.2.2 新增列，不删除接管前已经存在的业务表。

详细迁移 runbook：`docs/DB_MIGRATIONS_V1.2.2.md`。

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
pytest
```

生产/准生产环境必须配置 `INTERNAL_API_TOKEN`；若启用飞书，还必须完整配置飞书验签参数。人工审批应使用按用户签发、数据库仅保存哈希的 `ApiCredential`，不要共享一个人工审批令牌。

详细开发基线见 `docs/ARCHITECTURE_V1.2.md`。
