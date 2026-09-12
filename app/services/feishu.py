from __future__ import annotations

import hashlib
import hmac
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from app.config import Settings
from app.enums import OrderSide, Role
from app.models import Fund, Order

ROLE_PERMISSIONS = {
    Role.READONLY: {"query"},
    Role.EDITOR: {"query", "modify"},
    Role.CONFIRMER: {"query", "modify", "confirm"},
    Role.ADMIN: {"query", "modify", "confirm", "admin"},
}


@dataclass
class ParsedCommand:
    intent: str
    value: Decimal | None = None
    ratio: Decimal | None = None


class FeishuSecurity:
    def __init__(self, settings: Settings):
        self.settings = settings

    def verify_event(self, timestamp: str, nonce: str, signature: str, body: bytes) -> bool:
        if not (self.settings.feishu_encrypt_key and timestamp and nonce and signature):
            return False
        try:
            ts = int(timestamp)
        except ValueError:
            return False
        if abs(int(time.time()) - ts) > self.settings.feishu_replay_window_seconds:
            return False
        expected = hashlib.sha256(
            (timestamp + nonce + self.settings.feishu_encrypt_key).encode() + body
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def verify_token(self, token: str | None) -> bool:
        expected = self.settings.feishu_verification_token
        if not expected or token is None:
            return False
        return hmac.compare_digest(token, expected)


class CommandParser:
    PLUS_RE = re.compile(r"金额\s*(?:加|增加|\+)\s*(\d+(?:\.\d+)?)")
    SET_RE = re.compile(r"(?:金额|改成|买)\s*(\d+(?:\.\d+)?)")

    def parse(self, text: str) -> ParsedCommand:
        text = text.strip()
        if "为什么" in text or "理由" in text:
            return ParsedCommand("why")
        if "现在就交易" in text or text in {"执行", "确认执行", "就这样"}:
            return ParsedCommand("simulate_now")
        if "取消" in text:
            return ParsedCommand("cancel")
        if "卖一半" in text or "减一半" in text:
            return ParsedCommand("set_ratio", ratio=Decimal("0.5"))
        match = self.PLUS_RE.search(text)
        if match:
            return ParsedCommand("add_amount", value=Decimal(match.group(1)))
        match = self.SET_RE.search(text)
        if match:
            return ParsedCommand("set_amount", value=Decimal(match.group(1)))
        return ParsedCommand("query")


class FeishuClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def send_webhook(self, card: dict[str, Any]) -> None:
        if not self.settings.feishu_webhook_url:
            return
        with httpx.Client(timeout=15) as client:
            response = client.post(
                self.settings.feishu_webhook_url,
                json={"msg_type": "interactive", "card": card},
            )
            response.raise_for_status()


def trade_card(
    order: Order,
    fund: Fund,
    risk_status: str = "待风控",
    data_timestamp: str = "-",
) -> dict[str, Any]:
    if order.side == OrderSide.BUY:
        color, label, font = "red", "🔴 买入", "red"
    elif order.side == OrderSide.SELL:
        color, label, font = "green", "🟢 卖出", "green"
    else:
        color, label, font = "blue", f"🔵 {order.side.value}", "blue"
    amount = f"¥{order.amount:,.2f}" if order.amount is not None else "-"
    shares = (
        f"{order.shares:,.4f} 份"
        if order.shares is not None
        else "预计份额待净值确认"
    )
    cutoff = order.cutoff_at.strftime("%H:%M") if order.cutoff_at else "-"
    expiry = order.expires_at.strftime("%H:%M") if order.expires_at else "-"
    content = (
        f"<font color='{font}'>**{label}｜{fund.code} {fund.name}**</font>\n"
        f"板块：{fund.board}　份额类别：{fund.share_class}\n"
        f"金额：{amount}　份额：{shares}\n"
        f"订单：`{order.id}`　版本：v{order.version}\n"
        f"截止：{cutoff}　卡片有效至：{expiry}\n"
        f"数据时间戳：{data_timestamp}　风控：{risk_status}\n"
        f"理由：{order.reason or '-'}\n"
        "> 本卡仅用于模拟盘；份额/金额在正式净值确认前均为预计值。"
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": color,
            "title": {"tag": "plain_text", "content": f"{label}｜AI 场外基金公司（模拟盘）"},
        },
        "elements": [
            {"tag": "markdown", "content": content},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "确认模拟"},
                        "type": "primary",
                        "value": {"action": "confirm", "order_id": order.id, "version": order.version},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看理由"},
                        "value": {"action": "why", "order_id": order.id, "version": order.version},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "取消"},
                        "type": "danger",
                        "value": {"action": "cancel", "order_id": order.id, "version": order.version},
                    },
                ],
            },
        ],
    }
