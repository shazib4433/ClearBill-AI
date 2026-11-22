"""Runtime configuration, macOS SSL CA auto-fix, and .env hot-reload for ClearBill AI."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import certifi
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
_last_dotenv_mtime: float | None = None


def _ensure_ssl_certs() -> None:
    """Ensure SSL_CERT_FILE and REQUESTS_CA_BUNDLE point to a valid CA bundle on macOS."""
    ca_bundle = certifi.where()
    if not os.path.exists(ca_bundle):
        return
    for var_name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        current = os.getenv(var_name, "").strip()
        if not current or not os.path.exists(current):
            os.environ[var_name] = ca_bundle


_ensure_ssl_certs()


def _reload_dotenv_if_changed() -> None:
    """Hot-reload .env whenever modified on disk (skipped when CLEARBILL_SKIP_DOTENV=1)."""
    global _last_dotenv_mtime
    if os.getenv("CLEARBILL_SKIP_DOTENV") == "1":
        return
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    mtime = env_path.stat().st_mtime
    if _last_dotenv_mtime is None or mtime != _last_dotenv_mtime:
        load_dotenv(env_path, override=True)
        _last_dotenv_mtime = mtime


_reload_dotenv_if_changed()


def _resolve_path(raw_path: str) -> Path:
    candidate = Path(raw_path)
    return candidate if candidate.is_absolute() else (BASE_DIR / candidate)


@dataclass(frozen=True)
class Settings:
    """Resolved runtime configuration for ClearBill AI."""

    base_dir: Path
    his_db_path: Path
    audit_db_path: Path
    skills_dir: Path
    gemini_api_key: str
    gemini_ingest_model: str
    gemini_fusion_model: str
    use_vertex_ai: bool

    @property
    def has_live_gemini_key(self) -> bool:
        key = (self.gemini_api_key or "").strip()
        if not key:
            return False
        lowered = key.lower()
        return not (
            lowered.startswith("your_")
            or lowered.startswith("replace_")
            or lowered in {"changeme", "none", "null", "test-key"}
        )

    @property
    def auth_mode(self) -> str:
        if not self.has_live_gemini_key:
            return "DEMO_FALLBACK"
        return "VERTEX_EXPRESS" if self.use_vertex_ai else "AI_STUDIO"


def get_settings() -> Settings:
    """Build current settings dynamically, hot-reloading .env if updated."""
    _ensure_ssl_certs()
    _reload_dotenv_if_changed()
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    vertex_flag = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").strip().lower()
    use_vertex = vertex_flag in ("1", "true", "yes")
    return Settings(
        base_dir=BASE_DIR,
        his_db_path=_resolve_path(os.getenv("HIS_DB_PATH", "database/mock_his.db")),
        audit_db_path=_resolve_path(os.getenv("AUDIT_DB_PATH", "database/clearbill_audit.db")),
        skills_dir=BASE_DIR / ".agents" / "skills",
        gemini_api_key=api_key,
        gemini_ingest_model=os.getenv("GEMINI_INGEST_MODEL", "gemini-3.5-flash-lite").strip(),
        gemini_fusion_model=os.getenv("GEMINI_FUSION_MODEL", "gemini-4-hydrogen").strip(),
        use_vertex_ai=use_vertex,
    )
