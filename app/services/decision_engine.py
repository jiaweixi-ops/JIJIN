from __future__ import annotations

import json
from datetime import datetime, timezone

from app.enums import DataQualityLevel, OrderSide
from app.schemas import DecisionPlan, ResearchPacket, SCHEMA_VERSION
from app.services.ai_gateway import AIUnavailable, ModelGateway

RESEARCH_SYSTEM = (
    "你是 Kimi 研究院。只整理用户已提供的可信材料中的可核验事实，不做最终交易裁决。"
    "每条事实必须带 evidence_id、来源、时间戳、方向、周期、置信度和反方证据标记；"
    "source_name/source_url 必须来自输入材料，不得编造、替换或补造来源。输出严格 JSON。"
)
STRUCTURE_SYSTEM = (
    "你是 Qwen 数据研究中心。对研究材料结构化、去重、标记冲突，保留原 evidence_id 与来源引用。"
    "不能新增输入中不存在的来源，不能改写账本、净值、费用或硬风控。输出严格 JSON。"
)
CIO_SYSTEM = (
    "你是 DeepSeek CIO。仅依据 evidence_id、结构化研究与 Python 确定性数据形成模拟盘候选方案。"
    "证据不足、引用缺失或数据 RED 必须 WAIT。不得绕过硬风控，不得声称已成交或自动批准。"
    "输出严格 JSON。"
)


class DecisionEngine:
    def __init__(self, gateway: ModelGateway):
        self.gateway = gateway

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def research(self, topic: str, source_material: str) -> ResearchPacket:
        return self.gateway.call_structured(
            "kimi",
            "research",
            "research-v1.3",
            SCHEMA_VERSION,
            ResearchPacket,
            RESEARCH_SYSTEM,
            json.dumps(
                {
                    "topic": topic,
                    "as_of": self._now().isoformat(),
                    "material": source_material,
                },
                ensure_ascii=False,
            ),
        )

    def structure(self, packet: ResearchPacket) -> ResearchPacket:
        return self.gateway.call_structured(
            "qwen",
            "structure",
            "structure-v1.3",
            SCHEMA_VERSION,
            ResearchPacket,
            STRUCTURE_SYSTEM,
            packet.model_dump_json(),
        )

    def decide(
        self,
        research: ResearchPacket,
        python_metrics: dict,
        data_quality: DataQualityLevel,
    ) -> DecisionPlan:
        now = self._now()
        if data_quality == DataQualityLevel.RED:
            return DecisionPlan(
                as_of=now,
                decision_id=f"blocked-{int(now.timestamp())}",
                market_regime="UNKNOWN",
                actions=[
                    {
                        "action": OrderSide.WAIT,
                        "reason": "数据质量 RED，禁止生成模拟交易候选",
                        "confidence": 1.0,
                    }
                ],
                summary="数据质量闸门阻断",
                data_quality=data_quality,
                abstain_reason="DATA_QUALITY_RED",
            )
        try:
            plan = self.gateway.call_structured(
                "deepseek",
                "cio",
                "cio-v1.3",
                SCHEMA_VERSION,
                DecisionPlan,
                CIO_SYSTEM,
                json.dumps(
                    {
                        "research": research.model_dump(mode="json"),
                        "python_metrics": python_metrics,
                        "data_quality": data_quality.value,
                    },
                    ensure_ascii=False,
                ),
            )
        except AIUnavailable:
            return DecisionPlan(
                as_of=now,
                decision_id=f"cio-down-{int(now.timestamp())}",
                market_regime="UNKNOWN",
                actions=[
                    {
                        "action": OrderSide.WAIT,
                        "reason": "CIO 模型不可用，暂停模拟交易",
                        "confidence": 1.0,
                    }
                ],
                summary="CIO unavailable",
                data_quality=data_quality,
                abstain_reason="CIO_UNAVAILABLE",
            )

        evidence_ids = {e.evidence_id for e in research.facts}
        for action in plan.actions:
            if action.action in {OrderSide.BUY, OrderSide.SELL, OrderSide.CONVERT} and (
                not action.evidence_ids
                or not set(action.evidence_ids).issubset(evidence_ids)
            ):
                raise ValueError("CIO 结论引用了不存在的证据，拒绝进入模拟订单层")
        return plan
