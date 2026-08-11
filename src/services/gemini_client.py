from __future__ import annotations

import json
from typing import Any, TypeVar

from pydantic import BaseModel

from src.config import Settings, get_settings


SchemaT = TypeVar("SchemaT", bound=BaseModel)


class GeminiConfigurationError(RuntimeError):
    pass


class GeminiRequestError(RuntimeError):
    pass


class GeminiModelUnavailableError(GeminiRequestError):
    pass


class GeminiClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.gemini_api_key or self.settings.gemini_api_key == "your_key_here":
            raise GeminiConfigurationError(
                "GEMINI_API_KEY is missing. Copy .env.example to .env and add a valid key."
            )
        try:
            from google import genai
        except ImportError as exc:
            raise GeminiConfigurationError(
                "The Google Gen AI SDK is not installed. Run: pip install -r requirements.txt"
            ) from exc
        self._client = genai.Client(api_key=self.settings.gemini_api_key)

    @staticmethod
    def image_part(image_bytes: bytes, mime_type: str):
        from google.genai import types

        return types.Part.from_bytes(data=image_bytes, mime_type=mime_type)

    def generate_structured(
        self,
        *,
        model: str,
        contents: Any,
        schema: type[SchemaT],
    ) -> tuple[SchemaT, dict]:
        from google.genai import types

        try:
            response = self._client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=0.1,
                ),
            )
            response_text = response.text or ""
            parsed = getattr(response, "parsed", None)
            if isinstance(parsed, schema):
                validated = parsed
            elif isinstance(parsed, dict):
                validated = schema.model_validate(parsed)
            else:
                cleaned = response_text.strip()
                if cleaned.startswith("```"):
                    cleaned = cleaned.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                validated = schema.model_validate_json(cleaned)
            raw = {
                "model": model,
                "text": response_text,
                "parsed": validated.model_dump(mode="json"),
                "usage_metadata": (
                    response.usage_metadata.model_dump(mode="json")
                    if getattr(response, "usage_metadata", None) is not None
                    and hasattr(response.usage_metadata, "model_dump")
                    else None
                ),
            }
            return validated, raw
        except Exception as exc:
            message = str(exc)
            lowered = message.lower()
            if "model" in lowered and any(token in lowered for token in ("not found", "unavailable", "unsupported", "404")):
                raise GeminiModelUnavailableError(
                    f"Gemini model '{model}' is unavailable for this API account. "
                    "Change the corresponding GEMINI_*_MODEL value in .env and restart the app."
                ) from exc
            try:
                detail = json.loads(message)
                message = detail.get("message", message)
            except (json.JSONDecodeError, AttributeError):
                pass
            raise GeminiRequestError(f"Gemini request failed for model '{model}': {message}") from exc
