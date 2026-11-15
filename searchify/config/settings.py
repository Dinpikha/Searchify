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

PROJECT_ROOT = Path(__file__).resolve().parents[2]

if load_dotenv is not None:
    load_dotenv(PROJECT_ROOT / ".env")


def _default_reports_dir() -> Path:
    return Path(
        os.getenv("SEARCHIFY_REPORTS_DIR", str(Path.home() / ".searchify" / "reports"))
    ).expanduser()


def _default_reviews_dir() -> Path:
    return Path(
        os.getenv("SEARCHIFY_REVIEWS_DIR", str(Path.home() / ".searchify/reviews"))
    ).expanduser()


def _default_plans_dir() -> Path:
    return Path(
        os.getenv("SEARCHIFY_PLANS_DIR", str(Path.home() / ".searchify/plans"))
    ).expanduser()


def _default_local_db_path() -> Path:
    return Path(
        os.getenv("SEARCHIFY_DB_PATH", str(Path.home() / ".searchify" / "searchify.db"))
    ).expanduser()


@dataclass(frozen=True)
class Settings:
    # Application
    APP_NAME: str = "Searchify"
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development")

    # Supabase
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

    # Ollama
    OLLAMA_HOST: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")

    SUMMARY_MODEL: str = os.getenv("SUMMARY_MODEL", "qwen2.5-coder:7b")

    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "embeddinggemma-2:270m")

    EMBEDDING_DIMENSIONS: int = 768

    # File indexing
    MAX_FILE_SIZE: int = 2 * 1024 * 1024

    SUPPORTED_EXTENSIONS: frozenset = TEXT_EXTENSIONS

    # Summarization
    SUMMARY_MAX_OUTPUT_TOKENS: int = 1200
    SUMMARY_VERSION: int = 1

    # Query history
    QUERY_RETENTION_DAYS: int = 30

    # Retrieval
    INITIAL_CANDIDATE_LIMIT: int = 10
    RERANKED_RESULT_LIMIT: int = 5

    # Processing
    EMBEDDING_BATCH_SIZE: int = 32
    SUPABASE_BATCH_SIZE: int = 50

    # Local synchronization history (never stores extracted content).
    REPORTS_DIR: Path = field(default_factory=_default_reports_dir)
    REVIEWS_DIR: Path = field(default_factory=_default_reviews_dir)
    PLANS_DIR: Path = field(default_factory=_default_plans_dir)
    BACKUP_PATH: str = os.getenv("SEARCHIFY_BACKUP_PATH", "") or None

    # Local database
    LOCAL_DB_PATH: Path = field(default_factory=_default_local_db_path)

    def validate(self):
        if not self.SUPABASE_URL:
            raise ValueError("SUPABASE_URL is missing.")

        if not self.SUPABASE_KEY:
            raise ValueError("SUPABASE_KEY is missing.")

        if self.EMBEDDING_DIMENSIONS != 768:
            raise ValueError("Embedding dimensions must match the Supabase schema.")

        return self


settings = Settings()
