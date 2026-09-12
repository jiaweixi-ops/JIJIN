# JIJIN — AI 场外基金公司 V1.2.1

个人研究与真实前瞻模拟盘系统：Kimi 做研究、Qwen 做结构化、DeepSeek 做 CIO 推理、Python 做账本/风控/费用/截止时间/对账，飞书作为主要交互入口。

> V1.2.1 **仅支持 SimulationBroker 模拟执行**，不连接任何真钱账户、不包含基金销售平台真实下单接口；不构成投资建议，不承诺收益。`LIVE_TRADING_ENABLED=true` 会被启动校验直接拒绝。

## V1.2.1 已接通

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
- pytest 回归测试与 GitHub Actions

## 明确未支持

- **CONVERT 基金转换：V1.2.1 直接拒绝创建**，避免未完成双腿结算时冻结份额；计划在后续版本单独实现。
- 自动实盘交易。
- 08:45 盘前简报、13:30 提醒、14:00 决策卡、20:30 月末任务目前仍只有调度入口；其中赎回在途现金结算已接入真实业务逻辑，其余业务编排继续迭代。
- Alembic 生产迁移、完整 Docker 加固等工程化工作仍后置。

## 时间与数据规则

- 领域逻辑使用 timezone-aware datetime；数据库读取到的 naive datetime 在本项目中统一解释为 **UTC**。
- 截止时间/卡片展示转换为 `TIMEZONE`（默认 `Asia/Shanghai`）。
- 未确认 NAV 可以作为研究估算，因此 `research_quality` 可能为 YELLOW；但正式模拟结算必须 `settlement_eligibility=true`，并且使用与该订单估值日匹配的 confirmed NAV。
- 非交易日提交的模拟候选可解析到下一开放交易日估值；这不代表已经成交。

## 快速开始

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
python scripts/seed_demo.py
uvicorn app.main:app --reload
pytest
```

生产/准生产环境必须配置 `INTERNAL_API_TOKEN`；若启用飞书，还必须完整配置飞书验签参数。人工审批应使用按用户签发、数据库仅保存哈希的 `ApiCredential`，不要共享一个人工审批令牌。

详细开发基线见 `docs/ARCHITECTURE_V1.2.md`。
