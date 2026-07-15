from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path.cwd() / ".env", override=False)


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return min(max(value, minimum), maximum)


def _float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return min(max(value, minimum), maximum)


@dataclass(frozen=True)
class GeminiSettings:
    enabled: bool
    api_key: str | None
    model: str
    timeout_seconds: int
    max_retries: int
    max_concurrency: int
    thinking_level: str = "medium"
    budget_inr: float = 100.0
    usd_inr_rate: float = 100.0
    input_price_usd_per_million: float = 1.5
    output_price_usd_per_million: float = 9.0
    max_candidates: int = 5
    max_requests: int = 7
    max_runtime_seconds: int = 300
    max_total_tokens: int = 60_000
    max_output_tokens: int = 8_192
    max_verifications: int = 2
    request_reserve_inr: float = 20.0

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.api_key)

    def public_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "max_concurrency": self.max_concurrency,
            "thinking_level": self.thinking_level,
            "budget_inr": self.budget_inr,
            "max_candidates": self.max_candidates,
            "max_requests": self.max_requests,
            "max_runtime_seconds": self.max_runtime_seconds,
            "max_total_tokens": self.max_total_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_verifications": self.max_verifications,
            "request_reserve_inr": self.request_reserve_inr,
        }


def get_gemini_settings(enabled: bool | None = None) -> GeminiSettings:
    selected = _bool_env("DECIDIAN_AI_REVIEW", False) if enabled is None else enabled
    return GeminiSettings(
        enabled=selected,
        api_key=os.getenv("GEMINI_API_KEY") or None,
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash").strip()
        or "gemini-3.5-flash",
        timeout_seconds=_int_env("GEMINI_TIMEOUT_SECONDS", 45, 10, 120),
        max_retries=_int_env("GEMINI_MAX_RETRIES", 1, 0, 2),
        max_concurrency=_int_env("GEMINI_MAX_CONCURRENCY", 1, 1, 1),
        thinking_level=(
            os.getenv("GEMINI_THINKING_LEVEL", "medium").strip().casefold()
            if os.getenv("GEMINI_THINKING_LEVEL", "medium").strip().casefold()
            in {"minimal", "low", "medium", "high"}
            else "medium"
        ),
        budget_inr=_float_env("GEMINI_BUDGET_INR", 100.0, 1.0, 10_000.0),
        usd_inr_rate=_float_env("GEMINI_USD_INR_RATE", 100.0, 1.0, 1_000.0),
        input_price_usd_per_million=_float_env(
            "GEMINI_INPUT_PRICE_USD_PER_MILLION", 1.5, 0.0, 1_000.0
        ),
        output_price_usd_per_million=_float_env(
            "GEMINI_OUTPUT_PRICE_USD_PER_MILLION", 9.0, 0.0, 10_000.0
        ),
        max_candidates=_int_env("GEMINI_MAX_CANDIDATES", 5, 1, 100),
        max_requests=_int_env("GEMINI_MAX_REQUESTS_PER_RUN", 7, 1, 200),
        max_runtime_seconds=_int_env(
            "GEMINI_MAX_RUNTIME_SECONDS", 300, 30, 3_600
        ),
        max_total_tokens=_int_env(
            "GEMINI_MAX_TOTAL_TOKENS", 60_000, 1_000, 10_000_000
        ),
        max_output_tokens=_int_env(
            "GEMINI_MAX_OUTPUT_TOKENS_PER_REQUEST", 8_192, 256, 65_536
        ),
        max_verifications=_int_env("GEMINI_MAX_VERIFICATIONS", 2, 0, 100),
        request_reserve_inr=_float_env(
            "GEMINI_REQUEST_RESERVE_INR", 20.0, 0.0, 10_000.0
        ),
    )
