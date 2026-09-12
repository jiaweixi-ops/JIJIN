from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timezone
from decimal import Decimal
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.hardening_models import AIUsageLedger
from app.models import ModelCallLog

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    base_url: str
    api_key: str
    model: str
    input_cost_per_million: Decimal = Decimal("0")
    output_cost_per_million: Decimal = Decimal("0")
    max_output_tokens: int = 4096


class AIUnavailable(RuntimeError):
    pass


class AIQuotaExceeded(RuntimeError):
    pass


def _strip_json_fence(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


class OpenAICompatibleProvider:
    def __init__(
        self,
        cfg: ProviderConfig,
        timeout: float = 60.0,
        max_attempts: int = 2,
    ):
        self.cfg = cfg
        self.timeout = timeout
        self.max_attempts = max_attempts

    def complete_json(
        self,
        system: str,
        user: str,
        temperature: float = 0.2,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.cfg.api_key or not self.cfg.model:
            raise AIUnavailable(f"{self.cfg.name} 未配置")

        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": self.cfg.max_output_tokens,
            "response_format": {"type": "json_object"},
        }
        started = time.perf_counter()
        last_error: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.post(
                        self.cfg.base_url.rstrip("/") + "/chat/completions",
                        headers={"Authorization": f"Bearer {self.cfg.api_key}"},
                        json=payload,
                    )
                    response.raise_for_status()
                body = response.json()
                choices = body.get("choices")
                if not choices:
                    raise ValueError("model response missing choices")
                content = choices[0].get("message", {}).get("content")
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("model response missing message content")
                raw = json.loads(_strip_json_fence(content))
                usage = dict(body.get("usage") or {})
                usage["latency_ms"] = int((time.perf_counter() - started) * 1000)
                return raw, usage
            except (httpx.HTTPError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < self.max_attempts:
                    time.sleep(0.25 * attempt)

        raise AIUnavailable(f"{self.cfg.name} 调用失败: {last_error}") from last_error


class ModelGateway:
    def __init__(
        self,
        settings: Settings,
        db: Session | None = None,
        *,
        quota_subject: str = "system",
        request_id: str | None = None,
    ):
        self.settings = settings
        self.db = db
        self.quota_subject = quota_subject or "system"
        self.request_id = request_id
        self.providers = {
            "kimi": OpenAICompatibleProvider(
                ProviderConfig(
                    "kimi",
                    settings.kimi_base_url,
                    settings.kimi_api_key,
                    settings.kimi_model,
                    Decimal(str(settings.kimi_input_cost_per_million)),
                    Decimal(str(settings.kimi_output_cost_per_million)),
                    settings.ai_max_output_tokens,
                )
            ),
            "qwen": OpenAICompatibleProvider(
                ProviderConfig(
                    "qwen",
                    settings.qwen_base_url,
                    settings.qwen_api_key,
                    settings.qwen_model,
                    Decimal(str(settings.qwen_input_cost_per_million)),
                    Decimal(str(settings.qwen_output_cost_per_million)),
                    settings.ai_max_output_tokens,
                )
            ),
            "deepseek": OpenAICompatibleProvider(
                ProviderConfig(
                    "deepseek",
                    settings.deepseek_base_url,
                    settings.deepseek_api_key,
                    settings.deepseek_model,
                    Decimal(str(settings.deepseek_input_cost_per_million)),
                    Decimal(str(settings.deepseek_output_cost_per_million)),
                    settings.ai_max_output_tokens,
                )
            ),
        }

    def _session_factory(self):
        if self.db is None:
            return None
        return sessionmaker(
            bind=self.db.get_bind(),
            autoflush=False,
            expire_on_commit=False,
        )

    @staticmethod
    def _utc_day_start(now: datetime | None = None) -> datetime:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        return datetime.combine(current.date(), dt_time.min, tzinfo=timezone.utc)

    def _estimated_cost(self, provider: str, usage: dict[str, Any]) -> Decimal:
        cfg = self.providers[provider].cfg
        input_tokens = Decimal(str(usage.get("prompt_tokens") or 0))
        output_tokens = Decimal(str(usage.get("completion_tokens") or 0))
        return (
            input_tokens * cfg.input_cost_per_million
            + output_tokens * cfg.output_cost_per_million
        ) / Decimal("1000000")

    def _projected_max_cost(self, provider: str, input_chars: int) -> Decimal:
        """Conservative pre-call budget reservation.

        We treat one input character as up to one token and reserve the configured
        maximum output tokens. This intentionally overestimates common prompts so
        a configured daily cost ceiling fails closed rather than overshooting.
        """
        cfg = self.providers[provider].cfg
        return (
            Decimal(input_chars) * cfg.input_cost_per_million
            + Decimal(cfg.max_output_tokens) * cfg.output_cost_per_million
        ) / Decimal("1000000")

    def _write_usage(
        self,
        *,
        provider: str,
        role_name: str,
        input_chars: int,
        usage: dict[str, Any],
        estimated_cost: Decimal,
        success: bool,
        denied: bool,
        error: str,
    ) -> None:
        factory = self._session_factory()
        if factory is None:
            return
        cfg = self.providers[provider].cfg
        try:
            with factory() as usage_db:
                usage_db.add(
                    AIUsageLedger(
                        quota_subject=self.quota_subject,
                        request_id=self.request_id,
                        provider=provider,
                        model=cfg.model,
                        role_name=role_name,
                        input_chars=input_chars,
                        input_tokens=usage.get("prompt_tokens"),
                        output_tokens=usage.get("completion_tokens"),
                        estimated_cost=estimated_cost,
                        cost_currency=self.settings.ai_cost_currency,
                        success=success,
                        denied=denied,
                        error=error,
                    )
                )
                usage_db.commit()
        except Exception:
            return

    def _enforce_quota(self, provider: str, role_name: str, input_chars: int) -> None:
        if input_chars > self.settings.ai_max_request_chars:
            message = (
                f"AI request too large: {input_chars} chars > "
                f"{self.settings.ai_max_request_chars}"
            )
            self._write_usage(
                provider=provider,
                role_name=role_name,
                input_chars=input_chars,
                usage={},
                estimated_cost=Decimal("0"),
                success=False,
                denied=True,
                error=message,
            )
            raise AIQuotaExceeded(message)

        factory = self._session_factory()
        if factory is None:
            return
        day_start = self._utc_day_start()
        with factory() as quota_db:
            used_calls = quota_db.scalar(
                select(func.count(AIUsageLedger.id)).where(
                    AIUsageLedger.quota_subject == self.quota_subject,
                    AIUsageLedger.created_at >= day_start,
                    AIUsageLedger.denied.is_(False),
                )
            ) or 0
            used_cost = quota_db.scalar(
                select(func.coalesce(func.sum(AIUsageLedger.estimated_cost), 0)).where(
                    AIUsageLedger.quota_subject == self.quota_subject,
                    AIUsageLedger.created_at >= day_start,
                    AIUsageLedger.denied.is_(False),
                )
            ) or Decimal("0")

        projected = self._projected_max_cost(provider, input_chars)
        if used_calls >= self.settings.ai_daily_max_calls_per_subject:
            message = "AI daily call quota exceeded"
        elif (
            self.settings.ai_daily_max_estimated_cost > 0
            and Decimal(str(used_cost)) + projected
            > Decimal(str(self.settings.ai_daily_max_estimated_cost))
        ):
            message = (
                f"AI daily estimated-cost quota would be exceeded "
                f"({self.settings.ai_cost_currency})"
            )
        else:
            return

        self._write_usage(
            provider=provider,
            role_name=role_name,
            input_chars=input_chars,
            usage={},
            estimated_cost=Decimal("0"),
            success=False,
            denied=True,
            error=message,
        )
        raise AIQuotaExceeded(message)

    def _write_log(
        self,
        provider: str,
        role_name: str,
        prompt_version: str,
        schema_version: str,
        input_hash: str,
        success: bool,
        usage: dict[str, Any],
        estimated_cost: Decimal,
        error: str,
    ) -> None:
        factory = self._session_factory()
        if factory is None:
            return
        cfg = self.providers[provider].cfg
        try:
            with factory() as log_db:
                log_db.add(
                    ModelCallLog(
                        provider=provider,
                        model=cfg.model,
                        role_name=role_name,
                        prompt_version=prompt_version,
                        schema_version=schema_version,
                        input_hash=input_hash,
                        success=success,
                        latency_ms=int(usage.get("latency_ms", 0)),
                        input_tokens=usage.get("prompt_tokens"),
                        output_tokens=usage.get("completion_tokens"),
                        estimated_cost=estimated_cost,
                        error=error,
                    )
                )
                log_db.commit()
        except Exception:
            return

    def call_structured(
        self,
        provider: str,
        role_name: str,
        prompt_version: str,
        schema_version: str,
        schema: type[T],
        system: str,
        user: str,
    ) -> T:
        if provider not in self.providers:
            raise AIUnavailable(f"unknown provider: {provider}")

        input_chars = len(system) + len(user)
        self._enforce_quota(provider, role_name, input_chars)
        input_hash = hashlib.sha256((system + "\n" + user).encode()).hexdigest()
        success = False
        error = ""
        usage: dict[str, Any] = {}
        estimated_cost = Decimal("0")
        try:
            raw, usage = self.providers[provider].complete_json(system, user)
            estimated_cost = self._estimated_cost(provider, usage)
            result = schema.model_validate(raw)
            success = True
            return result
        except AIUnavailable as exc:
            error = str(exc)
            raise
        except ValidationError as exc:
            error = str(exc)
            raise AIUnavailable(f"{provider} 输出不符合 Schema: {exc}") from exc
        except Exception as exc:
            error = str(exc)
            raise AIUnavailable(f"{provider} 非预期失败: {exc}") from exc
        finally:
            self._write_log(
                provider,
                role_name,
                prompt_version,
                schema_version,
                input_hash,
                success,
                usage,
                estimated_cost,
                error,
            )
            self._write_usage(
                provider=provider,
                role_name=role_name,
                input_chars=input_chars,
                usage=usage,
                estimated_cost=estimated_cost,
                success=success,
                denied=False,
                error=error,
            )
