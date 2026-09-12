from __future__ import annotations

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "dev"
    database_url: str = "sqlite:///./jijin.db"
    timezone: str = "Asia/Shanghai"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = ""
    qwen_api_key: str = ""
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = ""
    kimi_api_key: str = ""
    kimi_base_url: str = "https://api.moonshot.cn/v1"
    kimi_model: str = ""
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""
    feishu_webhook_url: str = ""
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
