from __future__ import annotations

import hashlib
import hmac
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings, get_settings
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


def _as_utc(value: datetime) -> datetime:
    # Persisted datetimes may round-trip through SQLite as naive UTC.
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_local_time(value: datetime | None, timezone_name: str) -> str:
    if value is None:
        return "-"
    return _as_utc(value).astimezone(ZoneInfo(timezone_name)).strftime("%H:%M")


def _format_percent(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{Decimal(str(value)) * Decimal('100'):.2f}%"
    except (InvalidOperation, ValueError):
        return "-"


def _format_money(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"¥{Decimal(str(value)):,.2f}"
    except (InvalidOperation, ValueError):
        return "-"


def _portfolio_risk_lines(order: Order, settings: Settings) -> list[str]:
    snapshot = (order.risk_snapshot or {}).get("portfolio_risk") or {}
    if not isinstance(snapshot, dict) or not snapshot:
        return []

    lines: list[str] = []
    projected_weight = snapshot.get("projected_single_fund_weight")
    if projected_weight is not None:
        lines.append(
            "单基金投影："
            f"{_format_percent(projected_weight)} / "
            f"上限 {_format_percent(settings.max_single_fund_weight)}"
        )

    projected_daily = snapshot.get("projected_daily_trade_ratio")
    if projected_daily is not None:
        lines.append(
            "当日交易投影："
            f"{_format_percent(projected_daily)} / "
            f"上限 {_format_percent(settings.max_daily_trade_ratio)}"
        )

    drawdown = snapshot.get("portfolio_drawdown")
    if drawdown is not None:
        lines.append(
            "组合回撤："
            f"{_format_percent(drawdown)} / "
            f"保护线 {_format_percent(settings.max_portfolio_drawdown)}"
        )

    loss_days = snapshot.get("consecutive_loss_days")
    if loss_days is not None:
        lines.append(
            f"连续亏损：{loss_days} / {settings.max_consecutive_loss_days} 天"
        )

    if order.side == OrderSide.BUY:
        available_after = snapshot.get("available_cash_after_reservations")
        soft_reserved = snapshot.get("soft_reserved_cash")
        if available_after is not None:
            lines.append(
                "软预留："
                f"已占用 {_format_money(soft_reserved)}，"
                f"剩余可用 {_format_money(available_after)}"
            )
    elif order.side == OrderSide.SELL:
        reserved_shares = snapshot.get("soft_reserved_sell_shares")
        if reserved_shares is not None:
            lines.append(f"其他待确认 SELL 已软预留：{reserved_shares} 份")

    if snapshot.get("valuation_complete") is False:
        missing = snapshot.get("missing_nav_fund_ids") or []
        suffix = f"（{len(missing)} 只持仓缺少已确认 NAV）" if missing else ""
        lines.append(f"⚠️ 组合估值不完整{suffix}")
    return lines


def trade_card(
    order: Order,
    fund: Fund,
    risk_status: str = "待风控",
    data_timestamp: str = "-",
    timezone_name: str | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    timezone_name = timezone_name or settings.timezone
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
    cutoff = _format_local_time(order.cutoff_at, timezone_name)
    expiry = _format_local_time(order.expires_at, timezone_name)
    risk_lines = _portfolio_risk_lines(order, settings)
    risk_block = ""
    if risk_lines:
        risk_block = "\n**组合风控快照**\n" + "\n".join(f"- {line}" for line in risk_lines)
    content = (
        f"<font color='{font}'>**{label}｜{fund.code} {fund.name}**</font>\n"
        f"板块：{fund.board}　份额类别：{fund.share_class}\n"
        f"金额：{amount}　份额：{shares}\n"
        f"订单：`{order.id}`　版本：v{order.version}\n"
        f"截止：{cutoff}　卡片有效至：{expiry}\n"
        f"数据时间戳：{data_timestamp}　风控：{risk_status}\n"
        f"理由：{order.reason or '-'}"
        f"{risk_block}\n"
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
