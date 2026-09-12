from __future__ import annotations

import hashlib, json, time
from dataclasses import dataclass
from typing import Any, TypeVar
import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session
from app.config import Settings
from app.models import ModelCallLog

T = TypeVar("T", bound=BaseModel)

@dataclass(frozen=True)
class ProviderConfig:
    name: str; base_url: str; api_key: str; model: str

class AIUnavailable(RuntimeError): pass

class OpenAICompatibleProvider:
    def __init__(self, cfg: ProviderConfig, timeout: float = 60.0): self.cfg = cfg; self.timeout = timeout
    def complete_json(self, system: str, user: str, temperature: float = 0.2) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.cfg.api_key or not self.cfg.model: raise AIUnavailable(f"{self.cfg.name} 未配置")
        payload = {"model": self.cfg.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "temperature": temperature, "response_format": {"type": "json_object"}}
        started = time.perf_counter()
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(self.cfg.base_url.rstrip("/") + "/chat/completions", headers={"Authorization": f"Bearer {self.cfg.api_key}"}, json=payload); r.raise_for_status()
        body = r.json(); content = body["choices"][0]["message"]["content"]; usage = body.get("usage", {}); usage["latency_ms"] = int((time.perf_counter()-started)*1000)
        return json.loads(content), usage

class ModelGateway:
    def __init__(self, settings: Settings, db: Session | None = None):
        self.db = db
        self.providers = {
            "kimi": OpenAICompatibleProvider(ProviderConfig("kimi", settings.kimi_base_url, settings.kimi_api_key, settings.kimi_model)),
            "qwen": OpenAICompatibleProvider(ProviderConfig("qwen", settings.qwen_base_url, settings.qwen_api_key, settings.qwen_model)),
            "deepseek": OpenAICompatibleProvider(ProviderConfig("deepseek", settings.deepseek_base_url, settings.deepseek_api_key, settings.deepseek_model)),
        }
    def call_structured(self, provider: str, role_name: str, prompt_version: str, schema_version: str, schema: type[T], system: str, user: str) -> T:
        input_hash = hashlib.sha256((system+"\n"+user).encode()).hexdigest(); success=False; error=""; usage={}
        try:
            raw, usage = self.providers[provider].complete_json(system, user); result = schema.model_validate(raw); success=True; return result
        except (ValidationError, ValueError, KeyError, httpx.HTTPError) as exc:
            error=str(exc); raise
        finally:
            if self.db is not None:
                cfg=self.providers[provider].cfg; self.db.add(ModelCallLog(provider=provider, model=cfg.model, role_name=role_name, prompt_version=prompt_version, schema_version=schema_version, input_hash=input_hash, success=success, latency_ms=int(usage.get("latency_ms",0)), input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"), error=error)); self.db.commit()
