# V1.2.2 运行与容器加固 Runbook

## 目标

V1.2.2 的第五批 hardening 只解决工程运行面：lint、容器最小权限、健康检查、Compose 安全默认值、基础日志与监控。它不改变投资逻辑，也不启用实盘。

## Ruff / CI

CI 现在执行：

```bash
ruff check .
python -m compileall -q app tests scripts alembic
pytest
```

Ruff 当前 Phase-1 门禁覆盖 `E9/F`：语法级错误、未定义名称、未使用/错误导入等 correctness 问题必须在 CI 阶段失败。`I/B/UP/DTZ/FURB` 等导入排序、Bugbear 与现代化规则保留到后续清理 PR 分批开启，避免为了“把 lint 一次清零”产生大面积机械改动，影响当前 hardening 的可审查性。

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
# 填写强随机 INTERNAL_API_TOKEN / POSTGRES_PASSWORD / DATABASE_URL

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

因此不会直接暴露到局域网或公网。若后续需要远程访问，应使用受控反向代理 / VPN / TLS，并继续保留应用层认证；不要简单改成 `0.0.0.0:8000`。

`api` 还启用：

- `restart: unless-stopped`
- `read_only: true`
- `/tmp` 独立 tmpfs
- `no-new-privileges`
- `cap_drop: ALL`

PostgreSQL 同样使用 `restart: unless-stopped`。Compose 不再内置 `jijin:jijin` 之类弱口令；缺少 `POSTGRES_PASSWORD` 或 `DATABASE_URL` 时配置直接失败。

## 健康与就绪

- `GET /health`：进程级 liveness，返回版本和 simulation-first 模式。
- `GET /ready`：readiness，检查数据库连通性和 Alembic revision。
- Docker HEALTHCHECK 使用 `/ready`。

Liveness 不应该执行昂贵的业务查询；readiness 才用于阻止未迁移/数据库故障实例接收流量。

## 日志与 Request ID

每个 HTTP 请求：

- 接受已有 `X-Request-ID`，或生成 UUID。
- 响应返回同一个 `X-Request-ID`。
- 记录 method、route template、status、duration_ms、request_id。
- 500 路径记录 exception stack。

日志等级由 `LOG_LEVEL` 控制，允许：`CRITICAL/ERROR/WARNING/INFO/DEBUG`。

监控标签使用 FastAPI route template，不把实际 order_id 等动态路径值放入 Prometheus label，避免高基数。

## Prometheus 指标

`/metrics/` 暴露 Prometheus 文本格式基础指标，包括：

- `jijin_http_requests_total{method,route,status}`
- `jijin_http_request_duration_seconds{method,route}`

生产环境应在网络层限制 `/metrics`，只允许监控系统访问；不要在公网匿名暴露整个服务。

建议首批告警：

- 5xx 比例连续 5 分钟 > 1%
- `/ready` 失败
- p95 HTTP 延迟明显高于基线
- 服务实例反复重启
- PostgreSQL unhealthy

订单重复、账本错误、对账差异仍属于业务 P0/P1 监控，继续使用现有审计/对账机制，不用 HTTP 指标替代。
