# JIJIN — AI 场外基金公司 V1.2

个人研究与真实前瞻模拟盘系统：Kimi 做研究、Qwen 做结构化、DeepSeek 做 CIO 推理、Python 做账本/风控/费用/截止时间/对账，飞书作为主要交互入口。

> V1.2 **仅支持 SimulationBroker 模拟执行**，不连接任何真钱账户、不包含券商/基金销售平台真实下单接口；不构成投资建议，不承诺收益。

## 已实现

- 订单状态机、事件、版本、幂等键与旧卡失效
- 场外基金逐基金截止时间 / 有效期
- 费用版本、持有批次、短持有风控
- 可用/冻结/在途资金与确认份额账本
- 四级对账：订单 / 资金 / 份额 / 净值
- GREEN / YELLOW / RED 数据质量闸门
- `SimulationBroker` 前瞻模拟确认，不使用未来净值
- DeepSeek / Qwen / Kimi 结构化 AI Gateway
- 飞书红买绿卖卡片、自然语言修改、会话/版本/权限规则
- 08:45、13:30、14:00、月末调度骨架
- pytest 核心测试与 GitHub Actions

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

详细开发基线见 `docs/ARCHITECTURE_V1.2.md`。
