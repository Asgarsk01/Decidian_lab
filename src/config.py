from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import os

from dotenv import load_dotenv
from pydantic import BaseModel


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


class Settings(BaseModel):
    project_root: Path = PROJECT_ROOT
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///data/decidian_local.db")
    storage_dir: Path = PROJECT_ROOT / os.getenv("STORAGE_DIR", "storage")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_text_model: str = os.getenv("GEMINI_TEXT_MODEL", "gemini-3.5-flash")
    gemini_vision_model: str = os.getenv("GEMINI_VISION_MODEL", "gemini-3.5-flash")

    @property
    def database_connect_url(self) -> str:
        if not self.database_url.startswith("sqlite:///"):
            return self.database_url
        raw_path = self.database_url.removeprefix("sqlite:///")
        db_path = Path(raw_path)
        if not db_path.is_absolute():
            db_path = self.project_root / db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{db_path.as_posix()}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

