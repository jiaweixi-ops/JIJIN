# V1.2.2 数据库迁移 Runbook

## 目标

从 V1.2.2 hardening 开始，JIJIN 的数据库 schema 由 Alembic 管理。应用启动不再调用 `Base.metadata.create_all()` 自动修改数据库。

## 标准部署顺序

1. 对目标数据库做可恢复备份。
2. 安装当前代码依赖。
3. 设置 `DATABASE_URL`。
4. 执行 `alembic upgrade head`。
5. 执行 `alembic current`，确认当前 revision 为代码 head。
6. 再启动 FastAPI 服务。

应用启动阶段会校验 `alembic_version`。数据库不是 Alembic 管理状态，或 revision 落后/超前于当前代码 head 时，服务会 fail-fast，而不是尝试自动建表或补列。

## Revision 20260912_01：接管基线

它同时支持两种场景。

### 新数据库

- 创建冻结的 V1.2.2 baseline schema。
- 创建 `alembic_version`。
- `feishu_callback` 从一开始就包含 `status_code`。

### 已有 V1.2.1 数据库

- 已存在表不重建、不清空。
- 缺失的 baseline 表按 frozen metadata 补齐。
- 如果 `feishu_callback` 已存在但没有 `status_code`，增加 `status_code INTEGER NOT NULL DEFAULT 200`。
- 已有 callback 记录得到默认状态码 200。

## Revision 20260912_02：凭证生命周期与 AI 用量

当前代码 head 为 `20260912_02`。该 revision：

- 给 `api_credential` 增加 `hash_version`、`token_prefix`、`created_by`、`rotated_from_id`、`expires_at`、`revoked_at`；
- 将既有凭证标记为 `hash_version=sha256`，用于平滑迁移；新签发凭证使用 `hmac-sha256`；
- 创建 `ai_usage_ledger`，按主体、模型、请求记录字符数、token、估算成本、成功/拒绝和错误原因；
- 增加凭证到期/吊销/轮换和 AI 用量查询所需索引。

旧 SHA-256 凭证不会在 migration 中失效，但应通过 `/credentials/{id}/rotate` 逐步轮换到 HMAC + pepper。`API_CREDENTIAL_PEPPER` 只保存在服务端配置中，不能写进数据库或客户端。

## SQLite

开发环境示例：

```bash
export DATABASE_URL=sqlite:///./jijin.db
alembic upgrade head
alembic current
```

Windows PowerShell：

```powershell
$env:DATABASE_URL = "sqlite:///./jijin.db"
alembic upgrade head
alembic current
```

## PostgreSQL

目标环境示例：

```bash
export DATABASE_URL='postgresql+psycopg://user:password@host:5432/jijin'
alembic upgrade head
alembic current
```

CI 使用 PostgreSQL 16 service 实际执行 fresh migration，并验证 `alembic current`。

## 回滚边界

`20260912_01` 是“接管基线”，不是传统从零开始的历史 revision。生产环境 schema 回退仍应以备份恢复为主。

`20260912_02` 的 downgrade 会删除 `ai_usage_ledger` 和新增的凭证生命周期字段，因此会丢失 V1.2.2 hardening 之后生成的用量审计、到期/吊销/轮换元数据。生产环境不应在没有备份和明确数据处置方案时执行该 downgrade。

## 后续规则

- 新增/删除/修改表、列、约束、索引：必须新建 Alembic revision。
- 不允许通过应用启动 `create_all()` 充当 migration。
- migration 必须同时通过 SQLite 与 PostgreSQL CI。
- 涉及资金、份额、订单、对账、凭证的 schema 变更，migration PR 必须附数据兼容说明和回滚策略。
