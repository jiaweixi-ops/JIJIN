from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import ModelCallLog

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    base_url: str
    api_key: str
    model: str


class AIUnavailable(RuntimeError):
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
    def __init__(self, settings: Settings, db: Session | None = None):
        self.db = db
        self.providers = {
            "kimi": OpenAICompatibleProvider(
                ProviderConfig(
                    "kimi",
                    settings.kimi_base_url,
                    settings.kimi_api_key,
                    settings.kimi_model,
                )
            ),
            "qwen": OpenAICompatibleProvider(
                ProviderConfig(
                    "qwen",
                    settings.qwen_base_url,
                    settings.qwen_api_key,
                    settings.qwen_model,
                )
            ),
            "deepseek": OpenAICompatibleProvider(
                ProviderConfig(
                    "deepseek",
                    settings.deepseek_base_url,
                    settings.deepseek_api_key,
                    settings.deepseek_model,
                )
            ),
        }

    def _write_log(
        self,
        provider: str,
        role_name: str,
        prompt_version: str,
        schema_version: str,
        input_hash: str,
        success: bool,
        usage: dict[str, Any],
        error: str,
    ) -> None:
        if self.db is None:
            return
        cfg = self.providers[provider].cfg
        LocalSession = sessionmaker(
            bind=self.db.get_bind(),
            autoflush=False,
            expire_on_commit=False,
        )
        try:
            with LocalSession() as log_db:
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

        input_hash = hashlib.sha256((system + "\n" + user).encode()).hexdigest()
        success = False
        error = ""
        usage: dict[str, Any] = {}
        try:
            raw, usage = self.providers[provider].complete_json(system, user)
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
                error,
            )
