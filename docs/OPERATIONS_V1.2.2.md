# V1.2.2 运行与安全加固 Runbook

## 目标

V1.2.2 hardening 解决数据库演进、认证凭证生命周期、AI 成本边界、可信数据质量、lint、容器最小权限、健康检查、Compose 安全默认值、基础日志与监控。它不改变投资策略边界，也不启用实盘。

## 上线前必须配置

生产/准生产至少设置：

```text
APP_ENV=prod
INTERNAL_API_TOKEN=<强随机服务令牌>
API_CREDENTIAL_PEPPER=<与服务令牌不同的强随机服务端密钥>
DATABASE_URL=<PostgreSQL URL>
LIVE_TRADING_ENABLED=false
```

`INTERNAL_API_TOKEN` 与 `API_CREDENTIAL_PEPPER` 不允许相同。pepper 只保存在服务端秘密配置中，不能写入数据库、日志、客户端或飞书卡片。

如果启用 AI，还应配置模型、API Key、调用上限和真实价格。价格未知时保持 0，系统仍执行请求大小和每日调用次数限制，但不能给出有意义的金额成本上限。

## ApiCredential 生命周期

人工主体不使用共享服务令牌。通过管理 API 为真实 `User` 签发独立凭证：

- `POST /credentials`：签发；明文 token 只在响应中出现一次。
- `GET /credentials/users/{user_id}`：查看生命周期元数据，不返回 hash/token。
- `POST /credentials/{id}/rotate`：生成新凭证并吊销旧凭证。
- `POST /credentials/{id}/revoke`：立即吊销。

新凭证由 `secrets.token_urlsafe(32)` 生成，数据库保存 `HMAC-SHA256(token, API_CREDENTIAL_PEPPER)`、token prefix、到期时间、轮换来源、吊销时间和最后使用时间。默认 TTL 由 `API_CREDENTIAL_DEFAULT_TTL_DAYS` 控制，上限由 `API_CREDENTIAL_MAX_TTL_DAYS` 控制。

V1.2.1 遗留的裸 SHA-256 hash 凭证只为平滑迁移保留兼容；应在 V1.2.2 上线后逐个 rotate。审批/紧急退出仍要求两个不同的真实用户主体，system principal 不能充当人工审批人。

建议运维告警：临近到期 7 天、已过期仍有请求、被吊销凭证继续尝试认证、同一用户存在过多活跃凭证。

## AI 配额与成本

AI Gateway 在调用模型前执行：

1. `AI_MAX_SOURCE_CHARS`：外部研究材料上限。
2. `AI_MAX_REQUEST_CHARS`：完整 prompt 上限。
3. `AI_MAX_OUTPUT_TOKENS`：传给模型的最大输出 token。
4. `AI_DAILY_MAX_CALLS_PER_SUBJECT`：按认证用户/system 主体计算的 UTC 日调用上限。
5. `AI_DAILY_MAX_ESTIMATED_COST`：按当前已用成本 + 本次保守最大成本判断是否允许继续调用。

成本价格使用每百万 token 的 input/output 单价，分别配置 DeepSeek、Qwen、Kimi。`AIUsageLedger` 记录 quota subject、request ID、模型、字符数、provider token、估算成本、成功/拒绝与错误；`ModelCallLog.estimated_cost` 同步记录单次模型成本。

`GET /decisions/quota` 可查看当前认证主体当日使用量和上限。配额拒绝返回 HTTP 429，不调用模型。

### 调用前预算投影

预算投影不是 tokenizer 精确预测，而是故意偏高的 fail-closed 上界：

- ASCII/窄字符：按 1 token / 字符预留；
- CJK、Kana、Hangul、全角标点等 East-Asian wide/full-width 字符：按 2 token / 字符预留；
- 输出始终预留完整 `AI_MAX_OUTPUT_TOKENS`。

因此英文通常明显偏保守，中文也不再采用“1 字符 = 1 token”的假设。该公式的目的只有一个：在已知预算边界内宁可提前拒绝，也不要乐观低估成本。

### Provider usage 缺失兜底

模型调用成功后优先使用 provider 返回的 `usage.prompt_tokens` / `usage.completion_tokens` 计算成本。若任一 token 字段：

- 不存在；
- 不是合法数字；
- 为负数；

则该字段使用保守兜底：输入使用上述 CJK-aware token 上界，输出使用完整 `AI_MAX_OUTPUT_TOKENS`。因此即使 provider 不返回 `usage`，当价格配置为非 0 时，本次 `estimated_cost` 仍会进入 `AIUsageLedger`，后续日累计成本闸门不会退化成只看“下一次投影”。

为了不把估算值伪装成 provider 实际值，`AIUsageLedger.input_tokens/output_tokens` 与 `ModelCallLog.input_tokens/output_tokens` 仍保存 provider 原始 token 字段；provider 没返回时这些列可以为 NULL，而 `estimated_cost` 使用保守估算。系统同时写 `provider_usage_missing_or_invalid` WARNING，方便运维追踪哪些供应商/模型经常缺 usage。

### 并发边界

V1.2.2 仍采用：

```text
读取主体当日累计 → 判断是否允许 → 调用模型 → 写 AIUsageLedger
```

这不是原子预算预留。在两个或多个 worker 同时通过检查时，调用次数或日成本可能小幅超限。对当前个人研究/低并发模拟盘，这是明确接受的 V1.2.2 边界，而不是“严格配额”。

若未来变成多用户或高并发服务，需要升级为 `quota_subject + UTC date` 唯一的日计数/预算行，在模型调用前执行原子条件更新/`SELECT ... FOR UPDATE` 预算预留；调用失败再按规则释放或冲销。不要仅在现有 aggregate query 外层加普通事务并宣称已解决竞态。

### 记账失败可观测性

`AIUsageLedger` / `ModelCallLog` 仍使用独立 session，避免日志提交污染订单事务。但持久化失败不再静默吞掉：会记录 `WARNING`，包含 provider、role、quota subject、request ID 和异常。这样即使记账后端故障导致额度统计暂时不完整，也能从日志/监控发现，而不是静默退化。

建议增加告警：

- `ai_usage_ledger_write_failed`
- `model_call_log_write_failed`
- `provider_usage_missing_or_invalid`
- AI 429 / denied usage 异常抬升

## DataQuality 数据源接线

> **升级后重要行为变化：V1.2.2 不再把基金规则状态默认视为可信。每只要参与研究交易候选/新订单的基金，都必须先注册已启用的数据源并摄入最新规则快照；否则 `DataQualityGate` 会按设计返回 RED。**

`DataQualityGate` 不再默认“交易状态已知/公告正常”。运行前需要先注册可信数据源：

```text
PUT /data-quality/sources/{name}
```

数据源字段：`priority`（数字越小越权威）、`sla_seconds`、`enabled`。随后通过：

```text
POST /data-quality/funds/{fund_id}/rules
```

写入基金的申购/赎回状态、限额、费率版本、公告抓取状态、关键字段缺失和观测时间。系统把每次观测保存为 `DataQuality` 快照，并记录审计日志。

质量规则：

- 无规则快照、数据源停用、超过当前数据源 SLA、关键字段缺失、费率变化未确认 → RED。
- 权威来源在冲突窗口内对申赎/限购/费率给出不同结果 → RED。
- 低优先级冲突来源不会覆盖当前有效基金规则，但冲突本身继续 RED，直到权威来源重新确认。
- 公告抓取失败 → YELLOW。
- 未确认 NAV 可用于研究展示时为 YELLOW，但不能满足 settlement eligibility。
- `GET /data-quality/funds/{fund_id}` 返回 `research_quality`、`settlement_eligibility` 和原因。

`RiskService` / `/orders/.../risk` 和 `/decisions/run` 使用同一持久化 DataQualityGate 结果，避免客户端伪造 GREEN。

`seed_demo.py` 会为演示基金创建数据源、规则快照和 confirmed NAV；它不能替代真实基金的生产摄入流程。升级已有数据库后，如果历史基金尚未接入规则数据源，出现 RED 是预期 fail-closed 行为，不应通过硬编码 GREEN 绕过。

## Ruff / CI

CI 执行：

```bash
ruff check .
python -m compileall -q app tests scripts alembic
pytest
```

Ruff 当前 Phase-1 门禁覆盖 `E9/F`：语法级错误、未定义名称、未使用/错误导入等 correctness 问题必须在 CI 阶段失败。`I/B/UP/DTZ/FURB` 等规则保留到后续清理 PR 分批开启，避免大面积机械改动影响 hardening 的可审查性。

CI 同时验证 SQLite/PostgreSQL Alembic migration，以及 Docker 镜像的非 root 用户和 HEALTHCHECK。

## Docker 镜像

容器默认：

- Python 3.12 slim。
- 运行用户 `app`，UID/GID 10001，不以 root 运行。
- 禁止写 `.pyc`，stdout/stderr 无缓冲。
- 镜像内包含 Alembic migration 文件。
- Docker HEALTHCHECK 请求 `http://127.0.0.1:8000/ready`。

`/ready` 会同时检查数据库连接和 `alembic_version == code head`。如果数据库不可达或 migration 未执行，返回 503。

## Docker Compose

建议：

```bash
cp .env.compose.example .env
# 填写强随机 INTERNAL_API_TOKEN / API_CREDENTIAL_PEPPER / POSTGRES_PASSWORD / DATABASE_URL

docker compose up --build -d
```

Compose 有三个服务：

1. `postgres`：不发布宿主机端口，只在内部网络可达。
2. `migrate`：等待 PostgreSQL healthy 后执行一次 `alembic upgrade head`，成功退出。
3. `api`：只有 migration 成功后才启动。

API 默认只绑定宿主机 loopback：

```text
127.0.0.1:${JIJIN_API_PORT:-8000}:8000
```

若后续需要远程访问，应使用受控反向代理 / VPN / TLS，并继续保留应用层认证；不要简单改成 `0.0.0.0:8000`。

`api` 还启用：

- `restart: unless-stopped`
- `read_only: true`
- `/tmp` 独立 tmpfs
- `no-new-privileges`
- `cap_drop: ALL`

PostgreSQL 同样使用 `restart: unless-stopped`。Compose 不再内置弱口令；缺少关键数据库配置时直接失败。

## 健康与就绪

- `GET /health`：进程级 liveness，返回版本和 simulation-first 模式。
- `GET /ready`：readiness，检查数据库连通性和 Alembic revision。
- Docker HEALTHCHECK 使用 `/ready`。

Liveness 不执行昂贵业务查询；readiness 用于阻止未迁移/数据库故障实例接收流量。

## 日志与 Request ID

每个 HTTP 请求：

- 接受已有 `X-Request-ID`，或生成 UUID。
- 响应返回同一个 `X-Request-ID`。
- 记录 method、route template、status、duration_ms、request_id。
- 500 路径记录 exception stack。

日志等级由 `LOG_LEVEL` 控制，允许：`CRITICAL/ERROR/WARNING/INFO/DEBUG`。监控标签使用 FastAPI route template，不把实际 order_id 等动态路径值放入 Prometheus label，避免高基数。

## Prometheus 指标

`/metrics/` 暴露 Prometheus 文本格式基础指标，包括：

- `jijin_http_requests_total{method,route,status}`
- `jijin_http_request_duration_seconds{method,route}`

生产环境应在网络层限制 `/metrics`，只允许监控系统访问。

建议首批告警：

- 5xx 比例连续 5 分钟 > 1%
- `/ready` 失败
- p95 HTTP 延迟明显高于基线
- 服务实例反复重启
- PostgreSQL unhealthy
- AI 429 / denied usage 异常抬升
- `ai_usage_ledger_write_failed` / `model_call_log_write_failed`
- `provider_usage_missing_or_invalid` 持续出现
- DataQuality RED 数量突增或官方数据源超过 SLA
- 人工凭证即将过期/已吊销后继续使用

订单重复、账本错误、对账差异仍属于业务 P0/P1 监控，继续使用现有审计/对账机制，不用 HTTP 指标替代。
