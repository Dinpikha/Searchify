"""
Searchify Application Configuration

This module defines all configurable settings for the Searchify application.
Settings are loaded from environment variables with sensible defaults.
A frozen dataclass ensures settings are immutable after initialization.

Environment variables can be set in a `.env` file at the project root,
or exported in the shell before running Searchify.

Configuration categories:
- Application metadata
- Supabase connection (cloud sync target)
- Ollama connection (local AI runtime)
- Model selection and parameters
- File indexing limits and supported formats
- Summarization parameters
- Query history retention
- Retrieval/search parameters
- Batch processing sizes
- Local filesystem paths for persistence
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from searchify.core.extractor import TEXT_EXTENSIONS

try:
    from dotenv import load_dotenv

    DOTENV_AVAILABLE = True
except ImportError:
    load_dotenv = None
    DOTENV_AVAILABLE = False

# Project root directory (two levels up from this file: searchify/config/ -> project root)
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Load environment variables from .env file if python-dotenv is available
if load_dotenv is not None:
    load_dotenv(PROJECT_ROOT / ".env")


def _default_reports_dir() -> Path:
    """
    Default directory for sync reports.
    Can be overridden with SEARCHIFY_REPORTS_DIR environment variable.
    """
    return Path(
        os.getenv("SEARCHIFY_REPORTS_DIR", str(Path.home() / ".searchify" / "reports"))
    ).expanduser()


def _default_reviews_dir() -> Path:
    """
    Default directory for review artifacts.
    Can be overridden with SEARCHIFY_REVIEWS_DIR environment variable.
    """
    return Path(
        os.getenv("SEARCHIFY_REVIEWS_DIR", str(Path.home() / ".searchify/reviews"))
    ).expanduser()


def _default_plans_dir() -> Path:
    """
    Default directory for organization plans.
    Can be overridden with SEARCHIFY_PLANS_DIR environment variable.
    """
    return Path(
        os.getenv("SEARCHIFY_PLANS_DIR", str(Path.home() / ".searchify/plans"))
    ).expanduser()


def _default_local_db_path() -> Path:
    """
    Default path for the local SQLite database.
    Can be overridden with SEARCHIFY_DB_PATH environment variable.
    """
    return Path(
        os.getenv("SEARCHIFY_DB_PATH", str(Path.home() / ".searchify" / "searchify.db"))
    ).expanduser()


@dataclass(frozen=True)
class Settings:
    """
    Immutable application settings container.

    All settings are loaded from environment variables at module import time.
    The `validate()` method must be called after instantiation to verify
    required settings are present and consistent.
    """

    # Application
    APP_NAME: str = "Searchify"
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development")

    # Supabase - Cloud synchronization target
    # Required for sync functionality; validated in validate()
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

    # Ollama - Local AI runtime
    # Must be a local loopback HTTP address (validated in OllamaClient)
    OLLAMA_HOST: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")

    # Model selection
    # SUMMARY_MODEL: LLM for generating file summaries (default: qwen2.5-coder:7b)
    SUMMARY_MODEL: str = os.getenv("SUMMARY_MODEL", "qwen2.5-coder:7b")

    # EMBEDDING_MODEL: Model for generating 768-dim vectors (default: embeddinggemma-2:270m)
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "embeddinggemma-2:270m")

    # Embedding vector dimensions - must match Supabase schema (vector(768))
    EMBEDDING_DIMENSIONS: int = 768

    # File indexing limits
    # Maximum file size to process (2 MB) - larger files are skipped
    MAX_FILE_SIZE: int = 2 * 1024 * 1024

    # Supported text file extensions for extraction
    SUPPORTED_EXTENSIONS: frozenset = TEXT_EXTENSIONS

    # Summarization parameters
    # Maximum tokens for summary output (controls summary length)
    SUMMARY_MAX_OUTPUT_TOKENS: int = 1200

    # Summary schema version - increment when summary format changes
    SUMMARY_VERSION: int = 1

    # Query history retention period in days
    QUERY_RETENTION_DAYS: int = 30

    # Retrieval parameters
    # Initial candidate limit for semantic search (pgvector RPC match_count)
    INITIAL_CANDIDATE_LIMIT: int = 10

    # Result limit after reranking (for future reranker integration)
    RERANKED_RESULT_LIMIT: int = 5

    # Batch processing sizes
    # Reserved batch setting; the current Embeddings.embed sends one text per call
    EMBEDDING_BATCH_SIZE: int = 32

    # Number of records to upsert in a single Supabase batch
    SUPABASE_BATCH_SIZE: int = 50

    # Local synchronization history directories (never stores extracted content)
    REPORTS_DIR: Path = field(default_factory=_default_reports_dir)
    REVIEWS_DIR: Path = field(default_factory=_default_reviews_dir)
    PLANS_DIR: Path = field(default_factory=_default_plans_dir)

    # Optional backup path for database
    BACKUP_PATH: str = os.getenv("SEARCHIFY_BACKUP_PATH", "") or None

    # Local SQLite database path
    LOCAL_DB_PATH: Path = field(default_factory=_default_local_db_path)

    def validate(self):
        """
        Validate required settings and consistency constraints.

        Raises:
            ValueError: If required settings are missing or inconsistent.
        """
        if not self.SUPABASE_URL:
            raise ValueError("SUPABASE_URL is missing.")

        if not self.SUPABASE_KEY:
            raise ValueError("SUPABASE_KEY is missing.")

        if self.EMBEDDING_DIMENSIONS != 768:
            raise ValueError("Embedding dimensions must match the Supabase schema.")

        return self


# Global settings instance - validated on first access via Settings.validate()
settings = Settings()
