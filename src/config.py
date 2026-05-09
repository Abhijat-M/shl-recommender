"""Centralized settings (pydantic-settings)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Loaded from .env or environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- LLM ---
    llm_provider: str = Field(
        default="",
        description="Which backend to use: 'gemini' or 'groq'. Empty = auto.",
    )
    groq_api_key: str = Field(default="", description="Groq API key.")
    groq_model: str = Field(default="llama-3.3-70b-versatile")
    gemini_api_key: str = Field(default="", description="Google AI Studio API key.")
    gemini_model: str = Field(default="gemini-flash-latest")
    gemini_fallback_models: str = Field(
        default="gemini-2.5-flash,gemini-2.0-flash,gemini-2.5-flash-lite",
        description=(
            "Comma-separated models to try if the primary returns "
            "429 RESOURCE_EXHAUSTED. Each has its own 20 RPD free-tier quota."
        ),
    )

    # --- Service ---
    app_host: str = Field(default="0.0.0.0")
    app_port: int = Field(default=8000)
    log_level: str = Field(default="INFO")

    # --- Retrieval ---
    embedding_model: str = Field(default="sentence-transformers/all-MiniLM-L6-v2")
    top_k_dense: int = Field(default=25)
    top_k_sparse: int = Field(default=25)
    top_k_final: int = Field(default=10)
    rrf_k: int = Field(default=60)

    # --- Agent ---
    max_turns: int = Field(default=8)
    llm_timeout_seconds: float = Field(default=20.0)

    # --- Catalog ---
    catalog_path: str = Field(default="data/catalog/catalog.json")
    index_dir: str = Field(default="data/index")
    shl_catalog_base_url: str = Field(
        default="http://www.shl.com/products/product-catalog/"
    )

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parent.parent

    @property
    def catalog_file(self) -> Path:
        p = Path(self.catalog_path)
        return p if p.is_absolute() else self.project_root / p

    @property
    def index_path(self) -> Path:
        p = Path(self.index_dir)
        return p if p.is_absolute() else self.project_root / p


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor."""
    return Settings()
