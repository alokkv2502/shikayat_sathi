"""Paths and settings shared by every Shikayat Saathi module. Values come from the environment / .env."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.getenv("SAATHI_DATA_DIR", ROOT / "data"))
RAW_DIR = Path(os.getenv("SAATHI_RAW_DIR", ROOT / "raw"))
LEGAL_DIR = RAW_DIR / "legal"
CACHE_DIR = DATA_DIR / "cache"
CASES_DIR = DATA_DIR / "cases"

SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY", "")
# Live SerpApi calls allowed per complaint / per eval run (cached responses are free). 0 or "none" = no cap.
_cap = os.getenv("SERPAPI_MAX_CREDITS", "12").strip().lower()
SERPAPI_MAX_CREDITS: int | None = None if _cap in ("", "0", "none") else int(_cap)

# LLM: Gemini via the google-genai SDK (Interactions API). Without a key, or with LLM_PROVIDER=none, the agents run
# their rule-based fallbacks.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "").lower()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
LLM_API = os.getenv("LLM_API", "generate_content").lower()  # or "interactions" (~3x slower per call, measured)

# Qdrant: local server (docker compose up -d). If unreachable, embedded on-disk mode is used (data/qdrant/).
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY") or None
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "shikayat_saathi_law")
QDRANT_LOCAL_PATH = DATA_DIR / "qdrant"
EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-small-en-v1.5")

# LoanLens (sibling project) supplies the RBI Digital Lending App directory for loan-app complaints.
LOANLENS_DIR = Path(os.getenv("LOANLENS_DIR", ROOT.parent / "Loanlens"))

for _d in (DATA_DIR, CACHE_DIR, CASES_DIR):
    _d.mkdir(parents=True, exist_ok=True)
