from __future__ import annotations

import logging
from functools import lru_cache

import httpx
from langchain_openai import ChatOpenAI

from trader.config import Settings, get_settings

logger = logging.getLogger(__name__)


class NousCatalogError(RuntimeError):
    """Raised when configured models are missing from the Portal catalog."""


def fetch_portal_model_ids(settings: Settings | None = None) -> set[str]:
    settings = settings or get_settings()
    if not settings.nous_api_key:
        raise NousCatalogError("NOUS_API_KEY is not set")

    url = settings.nous_base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {settings.nous_api_key}"}
    with httpx.Client(timeout=30.0) as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        payload = response.json()

    data = payload.get("data") or payload.get("models") or []
    ids: set[str] = set()
    for item in data:
        if isinstance(item, str):
            ids.add(item)
        elif isinstance(item, dict):
            mid = item.get("id") or item.get("model")
            if mid:
                ids.add(str(mid))
    return ids


def verify_configured_models(settings: Settings | None = None) -> dict[str, str]:
    """Fail fast if any role model is absent from GET /v1/models."""
    settings = settings or get_settings()
    model_map = settings.model_map()
    if settings.skip_nous_catalog_check or settings.allow_mock_llm:
        logger.warning("Skipping Nous catalog verification")
        return model_map
    if not settings.nous_api_key:
        raise NousCatalogError("NOUS_API_KEY is required for catalog verification")

    available = fetch_portal_model_ids(settings)
    missing = {
        role: model_id
        for role, model_id in model_map.items()
        if model_id not in available
    }
    if missing:
        detail = ", ".join(f"{role}={mid}" for role, mid in missing.items())
        raise NousCatalogError(
            f"Configured models missing from Nous Portal catalog: {detail}. "
            "Confirm IDs at https://portal.nousresearch.com/models"
        )
    logger.info("Nous catalog check passed for %d roles", len(model_map))
    return model_map


@lru_cache
def get_chat_model(role: str) -> ChatOpenAI:
    settings = get_settings()
    model_map = settings.model_map()
    if role not in model_map:
        raise KeyError(f"Unknown model role: {role}")
    if not settings.nous_api_key and not settings.allow_mock_llm:
        raise NousCatalogError("NOUS_API_KEY is not set")

    return ChatOpenAI(
        model=model_map[role],
        api_key=settings.nous_api_key or "mock",
        base_url=settings.nous_base_url,
        temperature=0.2,
        max_retries=2,
    )
