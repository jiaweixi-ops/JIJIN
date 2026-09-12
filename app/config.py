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

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = ""
    qwen_api_key: str = ""
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = ""
    kimi_api_key: str = ""
    kimi_base_url: str = "https://api.moonshot.cn/v1"
    kimi_model: str = ""

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
        if env in {"prod", "staging"} and not self.internal_api_token:
            raise RuntimeError("INTERNAL_API_TOKEN is required outside dev/test")
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
