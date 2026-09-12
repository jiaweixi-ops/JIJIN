# AI 场外基金公司 V1.2.1 — 开发基线

## 1. 产品边界

V1.2.1 定位为**个人研究 + 前瞻模拟盘工具**，默认 `SimulationBroker`，不连接真钱账户、不自动提交真实交易。`LIVE_TRADING_ENABLED=true` 会被启动校验拒绝。所有输出使用“研究观点 / 候选方案 / 模拟执行”措辞，不承诺收益。

用户需要完成风险测评、适当性匹配和免责声明确认。风险测评与免责声明均版本化、有有效期、有审计记录。若未来公开收费、自动实盘、代客操作或面向他人提供个性化建议，应重新进行合规、资质、数据授权和安全评估。

## 2. AI 分工与可达链路

- **Kimi：研究院** — 新闻、政策、公告、基金经理、长文档与反方证据；只生成可核验事实。
- **Qwen：数据研究中心** — 结构化、去重、冲突标记、资料抽取；不能改账本、费率或硬风控。
- **DeepSeek：CIO** — 读取 Kimi/Qwen/Python 输出形成模拟候选方案；必须引用 `evidence_id`。证据不足、数据 RED 或 CIO 不可用时默认 WAIT，不允许用其他模型替代 CIO。
- **Python：确定性引擎** — 账本、费用、份额、仓位、净值、风险、截止时间、对账、模拟确认。

AI 输出使用 Schema v1.2.1。模型版本、提示词版本、Token、耗时和错误进入 `model_call_log`，日志使用独立数据库 session，不能提交业务 session 的挂起改动。

受认证的 `POST /decisions/run` 已把 `DataQualityGate → Kimi research → Qwen structure → DeepSeek CIO` 串成可达链路。RED 数据质量在进入外部模型前直接产生 WAIT 结果。

## 3. 每日节奏

- 08:45：盘前简报调度入口。
- 13:30：提前截止/特殊基金提醒调度入口。
- 14:00：模拟交易候选卡调度入口。
- 晚间/到账时点：正式净值到达后确认模拟订单并更新账本；赎回现金按 T+N 到账规则保持 `in_transit_cash`。
- 每 15 分钟（工作日 08:00–22:59）：执行到期在途赎回现金结转，幂等地把到期 `SELL_IN_TRANSIT` 转入 `available_cash`。
- 20:30：月末检查调度入口。

除在途现金结算外，08:45、13:30、14:00、20:30 目前仍主要是调度入口，完整业务编排继续迭代，不把“有定时任务”误写成“完整通知链路已完成”。

## 4. 时间、交易截止与估值日

交易窗口由 `销售平台 × 基金 × 支付渠道 × 交易日历` 决定，而不是统一 15:00。`Fund.cut_off_time` 是基础字段，生产数据源应覆盖平台例外。

时间约定：

1. 领域逻辑使用 timezone-aware datetime。
2. 持久化/跨服务统一采用 UTC 语义；由于 SQLite 可能把 aware datetime 读回 naive，**本项目凡从数据库得到的 naive datetime 一律解释为 UTC**。
3. 截止规则与展示边界再转 `TIMEZONE`（默认 `Asia/Shanghai`）或对应销售平台时区。
4. 飞书卡片必须展示本地截止/有效时间；API `OrderView` 至少返回带 UTC offset 的时间，禁止返回无法判别时区的裸时间。

订单有效期默认不晚于 `cut_off_time - 10min`；修改订单不会延长截止时间，新版本必须重新风控。超时未确认进入 `EXPIRED`，不自动顺延。

模拟确认先计算订单唯一 `valuation_date`：提交发生在开放交易日且截止前时使用当日，截止后或非交易日滚到下一开放交易日。结算不能使用任意 `nav_date <= today` 的净值，只能使用 `fund_id + valuation_date + confirmed=true` 的 `NavConfirm`。QDII/FOF 可处于“估值日已确定、净值尚未公布”的 WAITING_NAV 状态。

## 5. 费用、份额类别与 lot allocation

短周期候选可优先评估 C 类 / 联接 C，但必须把销售服务费、申购费、赎回费等纳入。申购前端收费按 `net = gross / (1 + rate)`、`fee = gross - net` 计算。费率按版本记录，变化未确认时研究质量进入 RED。

持仓按 `HoldingLot` 批次保存。风险和费用共同使用同一 lot allocation 逻辑；默认 FIFO，先 dry-run 本次赎回实际会消耗哪些 lot，再判断短持有规则与费用，结果快照到订单，避免风控和结算两套口径。

`emergency_exit=true` 是显式硬风控例外流程：第一次审批进入紧急确认状态并保存惩罚性费用快照与发起人；第二次必须由**另一个真实用户主体**批准，写入 `emergency_approved_by` 和 `AuditLog`。同一主体即使持有多个令牌也不能完成双签。

## 6. 在途资产与正式账本

账户区分：`available_cash`、`frozen_cash`、`in_transit_cash`、已确认基金份额与冻结份额。盘中估值只用于研究展示，不进入正式账本。

申购：冻结现金 → 匹配估值日的 confirmed NAV → 计算实际份额 → 释放冻结现金并落 `TradeFill`。

赎回：冻结实际 lot → confirmed NAV → 计算费用和净额 → 净额进入 `in_transit_cash` → 根据 `confirm_days_sell` / 平台规则和交易日历计算 `available_at` → 调度任务到期后转入 `available_cash`。在途资金在到账前不得用于新买入。

## 7. 订单状态机与版本

主链：`SUGGESTED → PENDING_RISK → PENDING_CONFIRM → APPROVED → SUBMITTED → IN_TRANSIT → CONFIRMED`。

紧急退出：`PENDING_CONFIRM → PENDING_EMERGENCY_CONFIRM → APPROVED`。

旁路包括：`RISK_REJECTED / MODIFIED / PARTIALLY_CONFIRMED / EXECUTION_FAILED / EXPIRED / CANCELLED / MANUAL_RECONCILED`。

关键规则：

1. 每单唯一 `order_id + version + idempotency_key`。
2. 状态变更使用条件更新 `WHERE id=:id AND version=:expected AND status=:from_status`；`rowcount != 1` 视为 stale version / 并发冲突。
3. 用户修改后产生新版本，旧飞书卡点击 409，并重新风控。
4. 飞书重复回调以 `event_id` 唯一约束幂等返回原结果。
5. 现金/份额冻结使用条件更新，避免并发双花。
6. 终态不能用普通事件反向修改；人工修复必须留审计轨迹。

### CONVERT 边界

**V1.2.1 不支持基金转换。** `OrderCreate` 和 `OrderService.create()` 直接返回 `CONVERT_NOT_SUPPORTED`，不会进入冻结或 `IN_TRANSIT`。旧文档中关于“V1.2 已支持 OUT/IN 双腿转换结算”的描述全部作废。双腿转换、失败回滚、平台费用与确认时间在后续版本单独设计。

## 8. 数据质量：研究质量与结算资格分离

`DataQualityGate` 输出两个维度：

- `research_quality: GREEN / YELLOW / RED`
- `settlement_eligibility: bool`

工程默认阈值（不是监管规则）：

- GREEN：关键字段齐全、无关键冲突、净值时间小于 36h、费率版本已确认、申赎状态已知。
- YELLOW：净值 36–72h、净值未确认、公告抓取异常等非关键缺口。允许研究，但必须显示警告。
- RED：净值超过 72h、缺少净值时间戳、未来时间戳、费率版本缺失/变化未确认、申赎/限购状态未知、关键源冲突或关键字段缺失。阻断新交易候选进入风控通过链路。

未确认 NAV 可以用于研究估算，因此 research 可能是 YELLOW；但正式模拟结算必须存在与订单估值日匹配的 confirmed `NavConfirm`。`settlement_eligibility=false` 本身不等于“不能形成研究候选”，这是有意设计。

## 9. 对账与解除阻断

四级对账目标仍是订单、资金、份额、净值。默认工程容忍度：现金 0.01 元、份额 0.0001、净值 1e-8；实际平台可配置。

`ReconciliationService` 的 create/compare/finish/resolve 已有受认证 API 入口。风险查询只应阻断**当前仍未解决的 blocking diff**；历史已解决差异保留审计但不永久锁死账户。人工 resolve 必须有真实用户主体、原因和 AuditLog。最后一个 blocking diff 解决后 run 进入 `RESOLVED`。

来源优先建议：官方/销售平台 API > 官方确认单/流水导出 > 手工导入 > 截图 OCR。OCR 只能作为候选数据，人工确认后才能进入正式账本。

## 10. SimulationBroker

模拟盘遵守场外交易核心约束：截止时间、开放状态、限购、lot、费率、确认延迟、在途资金和交易日历。**不接受调用方传入 NAV，也不使用未来净值成交。**

确认条件：

1. 订单处于可确认状态。
2. 已冻结相应现金或份额。
3. 已解析并冻结 `valuation_date`。
4. 数据库存在该基金、该估值日、`confirmed=true` 的 `NavConfirm`。
5. 不满足时返回 WAITING_NAV，而不是拿最近历史净值替代。

## 11. 飞书安全、卡片和会话

买入使用红色主题 + `🔴 买入`，卖出使用绿色主题 + `🟢 卖出`，同时保留文字标签。

入口链：验签 → 时间窗口 → event_id 防重放 → 飞书 open_id 映射真实 User → 会话未过期 → 权限 → order_id/version。

公开 `/feishu/command?open_id=...` 已移除；自然语言命令只从已验签事件进入。交易卡发送或用户点击卡片会创建/刷新 `FeishuSession`，绑定 `open_id + order_id + version`，TTL 由 `FEISHU_SESSION_TTL_MINUTES` 控制。自然语言“金额加1000”“卖一半”“为什么”“现在就交易”等只能作用于未过期且版本匹配的当前订单。

受认证的 `/feishu/orders/{order_id}/send-card` 已把 `trade_card` / `FeishuClient` 接入调用链，并创建会话与 `NotificationLog`。按钮点击同样会绑定/刷新会话。

V1.2.1 仍然只做模拟批准/模拟执行，不连接真钱交易接口。

## 12. 内部认证与双人审批

共享 `INTERNAL_API_TOKEN` 只代表系统自动化主体，`actor_id=None`，不能完成需要人工身份的订单审批或对账 resolve。

人工操作使用 `ApiCredential`：每个凭证绑定一个真实、active 的 `User`，数据库只保存 SHA-256 token hash。服务端从凭证反查 User 后得到 `principal.actor_id=user.id`；调用方不再能通过 `X-Actor-Id` 自填身份。

因此紧急退出的双人审批条件是两个不同真实 User ID，而不是两个不同字符串请求头。

## 13. 监控与验收

关键 SLI：任务成功率、数据新鲜度、AI Schema 通过率、飞书投递成功率、订单积压、对账差异率、重复回调幂等率。

进入下一阶段前至少满足：

- CI 的 compileall + pytest 全绿。
- AI Schema 不合格输出直接拒绝。
- 重复回调/重复提交不产生重复订单或重复账务。
- 旧版本卡片 100% 拒绝。
- RED research quality 阻断新模拟交易候选。
- 结算必须 valuation_date 精确匹配 confirmed NAV。
- 未解决 blocking 对账差异阻断新单，resolve 后恢复。
- 赎回现金到账前保持在途，到账后仅结转一次。
- 紧急退出同一真实用户不能双签。
- 时间展示测试覆盖“UTC 07:00 = Asia/Shanghai 15:00”等边界。
- 连续前瞻运行至少 4–6 周，并完整经历一次月结补记。

## 14. 后续工程项

以下不作为本次 V1.2.1 P0 收尾的一部分：Alembic 生产迁移、Docker 非 root/healthcheck、compose 密钥管理、CI ruff gate、完整监控告警与生产部署基线。进入准生产部署前必须完成。

真实资金接口继续后置，不在 V1.2.1 范围。
