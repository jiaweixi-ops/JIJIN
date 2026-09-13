from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "prod"
    database_url: str = "sqlite:///./jijin.db"
    timezone: str = "Asia/Shanghai"
    log_level: str = "INFO"
    internal_api_token: str = ""
    live_trading_enabled: bool = False

    # Human API credentials. New credentials are HMAC-SHA256 hashed with this
    # server-side pepper; the plaintext token is shown exactly once at issue/rotation.
    api_credential_pepper: str = ""
    api_credential_default_ttl_days: int = 90
    api_credential_max_ttl_days: int = 365

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = ""
    qwen_api_key: str = ""
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = ""
    kimi_api_key: str = ""
    kimi_base_url: str = "https://api.moonshot.cn/v1"
    kimi_model: str = ""

    # AI budget controls. Cost rates are in AI_COST_CURRENCY per 1M tokens.
    # A zero price means "unknown price"; call-count/size quotas still apply.
    ai_cost_currency: str = "CNY"
    ai_max_source_chars: int = 100_000
    ai_max_request_chars: int = 120_000
    ai_max_output_tokens: int = 4096
    ai_daily_max_calls_per_subject: int = 30
    ai_daily_max_estimated_cost: float = 50.0
    deepseek_input_cost_per_million: float = 0.0
    deepseek_output_cost_per_million: float = 0.0
    qwen_input_cost_per_million: float = 0.0
    qwen_output_cost_per_million: float = 0.0
    kimi_input_cost_per_million: float = 0.0
    kimi_output_cost_per_million: float = 0.0

    feishu_enabled: bool = False
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""
    feishu_webhook_url: str = ""
    feishu_replay_window_seconds: int = 300
    feishu_session_ttl_minutes: int = 30

    default_cutoff_buffer_minutes: int = 10
    order_stale_minutes: int = 60
    risk_profile_valid_days: int = 365
    nav_red_after_hours: int = 72
    nav_yellow_after_hours: int = 36
    data_source_conflict_window_minutes: int = 15
    max_single_fund_weight: float = 0.20
    max_daily_trade_ratio: float = 0.30
    max_portfolio_drawdown: float = 0.12
    max_consecutive_loss_days: int = 5
    short_hold_days: int = 7

    def validate_runtime(self) -> None:
        env = self.app_env.lower().strip()
        if env not in {"prod", "staging", "dev", "test"}:
            raise RuntimeError(f"unsupported APP_ENV={self.app_env!r}")
        if self.log_level.upper().strip() not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise RuntimeError(f"unsupported LOG_LEVEL={self.log_level!r}")
        if self.live_trading_enabled:
            raise RuntimeError(
                "V1.2.2 does not implement live trading; LIVE_TRADING_ENABLED must be false"
            )
        if env in {"prod", "staging"}:
            if not self.internal_api_token:
                raise RuntimeError("INTERNAL_API_TOKEN is required outside dev/test")
            if not self.api_credential_pepper:
                raise RuntimeError("API_CREDENTIAL_PEPPER is required outside dev/test")
            if self.internal_api_token == self.api_credential_pepper:
                raise RuntimeError("INTERNAL_API_TOKEN and API_CREDENTIAL_PEPPER must be different")
        if self.api_credential_default_ttl_days < 1:
            raise RuntimeError("API_CREDENTIAL_DEFAULT_TTL_DAYS must be >= 1")
        if self.api_credential_max_ttl_days < self.api_credential_default_ttl_days:
            raise RuntimeError(
                "API_CREDENTIAL_MAX_TTL_DAYS must be >= API_CREDENTIAL_DEFAULT_TTL_DAYS"
            )
        if self.ai_max_source_chars < 1 or self.ai_max_request_chars < 1:
            raise RuntimeError("AI request/source character limits must be positive")
        if self.ai_max_output_tokens < 1:
            raise RuntimeError("AI_MAX_OUTPUT_TOKENS must be >= 1")
        if self.ai_daily_max_calls_per_subject < 1:
            raise RuntimeError("AI_DAILY_MAX_CALLS_PER_SUBJECT must be >= 1")
        if self.ai_daily_max_estimated_cost < 0:
            raise RuntimeError("AI_DAILY_MAX_ESTIMATED_COST must be >= 0")
        if self.data_source_conflict_window_minutes < 0:
            raise RuntimeError("DATA_SOURCE_CONFLICT_WINDOW_MINUTES must be >= 0")
        if self.feishu_enabled:
            missing = [
                name
                for name, value in {
                    "FEISHU_APP_ID": self.feishu_app_id,
                    "FEISHU_APP_SECRET": self.feishu_app_secret,
                    "FEISHU_VERIFICATION_TOKEN": self.feishu_verification_token,
                    "FEISHU_ENCRYPT_KEY": self.feishu_encrypt_key,
                }.items()
                if not value
            ]
            if missing:
                raise RuntimeError("Feishu enabled but missing: " + ", ".join(missing))


@lru_cache
def get_settings() -> Settings:
    return Settings()
