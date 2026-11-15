from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    nous_api_key: str = Field(default="", alias="NOUS_API_KEY")
    nous_base_url: str = Field(
        default="https://inference-api.nousresearch.com/v1",
        alias="NOUS_BASE_URL",
    )

    model_fundamental: str | None = Field(default=None, alias="MODEL_FUNDAMENTAL")
    model_technical: str | None = Field(default=None, alias="MODEL_TECHNICAL")
    model_sentiment: str | None = Field(default=None, alias="MODEL_SENTIMENT")
    model_macro: str | None = Field(default=None, alias="MODEL_MACRO")
    model_portfolio_manager: str | None = Field(
        default=None, alias="MODEL_PORTFOLIO_MANAGER"
    )
    model_risk: str | None = Field(default=None, alias="MODEL_RISK")

    database_url: str = Field(
        default="postgresql+asyncpg://trader:trader@localhost:5432/trader",
        alias="DATABASE_URL",
    )
    database_url_sync: str = Field(
        default="postgresql://trader:trader@localhost:5432/trader",
        alias="DATABASE_URL_SYNC",
    )

    ibkr_trading_mode: Literal["paper", "live"] = Field(
        default="paper", alias="IBKR_TRADING_MODE"
    )
    ibkr_live_orders_enabled: bool = Field(
        default=False, alias="IBKR_LIVE_ORDERS_ENABLED"
    )
    ibkr_host: str = Field(default="127.0.0.1", alias="IBKR_HOST")
    ibkr_port: int = Field(default=4004, alias="IBKR_PORT")
    ibkr_client_id: int = Field(default=7, alias="IBKR_CLIENT_ID")
    ibkr_account_id: str = Field(default="", alias="IBKR_ACCOUNT_ID")
    tws_userid: str = Field(default="", alias="TWS_USERID")
    tws_password: str = Field(default="", alias="TWS_PASSWORD")

    max_position_pct: float = Field(default=0.10, alias="MAX_POSITION_PCT")
    price_band_pct: float = Field(default=0.02, alias="PRICE_BAND_PCT")
    proposal_ttl_minutes: int = Field(default=30, alias="PROPOSAL_TTL_MINUTES")
    exit_max_loss_pct: float = Field(default=0.08, alias="EXIT_MAX_LOSS_PCT")
    exit_review_hour_utc: int = Field(default=21, alias="EXIT_REVIEW_HOUR_UTC")
    horizon_review_cap: int = Field(default=3, alias="HORIZON_REVIEW_CAP")
    starting_cash: float = Field(default=100_000.0, alias="STARTING_CASH")
    restricted_tickers: str = Field(default="", alias="RESTRICTED_TICKERS")

    operator_user: str = Field(default="operator", alias="OPERATOR_USER")
    operator_password: str = Field(default="change_me", alias="OPERATOR_PASSWORD")

    skip_nous_catalog_check: bool = Field(
        default=False, alias="SKIP_NOUS_CATALOG_CHECK"
    )
    allow_mock_llm: bool = Field(default=False, alias="ALLOW_MOCK_LLM")
    allow_mock_broker: bool = Field(default=False, alias="ALLOW_MOCK_BROKER")

    @field_validator("ibkr_live_orders_enabled", mode="before")
    @classmethod
    def _parse_bool(cls, value: object) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def restricted_set(self) -> set[str]:
        if not self.restricted_tickers.strip():
            return set()
        return {t.strip().upper() for t in self.restricted_tickers.split(",") if t.strip()}

    def model_map(self) -> dict[str, str]:
        path = CONFIG_DIR / "models.yaml"
        raw: dict[str, str] = {}
        if path.exists():
            loaded = yaml.safe_load(path.read_text()) or {}
            raw.update({str(k): str(v) for k, v in loaded.items()})
        overrides = {
            "fundamental": self.model_fundamental,
            "technical": self.model_technical,
            "sentiment": self.model_sentiment,
            "macro": self.model_macro,
            "portfolio_manager": self.model_portfolio_manager,
            "risk": self.model_risk,
        }
        for key, value in overrides.items():
            if value:
                raw[key] = value
        return raw


@lru_cache
def get_settings() -> Settings:
    return Settings()
