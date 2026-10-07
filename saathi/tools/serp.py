"""SerpApi client with a disk cache and a credit guard.

Every response is cached at data/cache/serp/<sha1(sorted params)>.json, so re-running a case or the
evaluation costs zero credits. Live calls are counted per process and in data/cache/serp/_ledger.json.

Engines used (serpapi.com/search-api):
    google          q, gl=in, hl=en, google_domain=google.co.in -> knowledge_graph, organic_results
    google_forums   q, gl=in, hl=en                            -> organic_results
    google_news     q, gl=in, hl=en                            -> news_results
    google_maps     q, type=search, gl=in, hl=en               -> local_results
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from config import CACHE_DIR, SERPAPI_API_KEY, SERPAPI_MAX_CREDITS

ENDPOINT = os.getenv("SERPAPI_ENDPOINT", "https://serpapi.com/search.json")  # override only for tests


class CreditLimitReached(RuntimeError):
    """Raised when a live call would exceed max_credits."""


class SerpApiError(RuntimeError):
    """SerpApi unreachable / timed out / returned an error. Callers record it and carry on."""


class NoApiKey(RuntimeError):
    """Raised when a response is not cached and SERPAPI_API_KEY is not set."""


class SerpClient:
    def __init__(self, api_key: str = SERPAPI_API_KEY, cache_dir: Path = CACHE_DIR / "serp",
                 max_credits: int | None = SERPAPI_MAX_CREDITS, offline: bool = False, verbose: bool = True):
        self.api_key = api_key
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_credits = max_credits
        self.offline = offline
        self.verbose = verbose
        self.live_calls = 0
        self.cache_hits = 0
        self.budget_start = 0  # live_calls value when the current budget (one complaint / one eval run) began
        self._lock = threading.Lock()
        self._tl = threading.local()  # per-thread live-call count (parallel graph nodes share this client)

    @staticmethod
    def cache_key(params: dict) -> str:
        clean = {k: str(v) for k, v in params.items() if k not in ("api_key", "no_cache", "output") and v is not None}
        return hashlib.sha1(json.dumps(clean, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _path(self, params: dict) -> Path:
        return self.cache_dir / f"{self.cache_key(params)}.json"

    def cached(self, params: dict) -> dict | None:
        p = self._path(params)
        if p.exists():
            try:
                return json.loads(p.read_text())["response"]
            except (json.JSONDecodeError, KeyError):
                return None
        return None

    def _ledger(self, params: dict) -> None:
        path = self.cache_dir / "_ledger.json"
        try:
            ledger = json.loads(path.read_text()) if path.exists() else {"total_live_calls": 0, "calls": []}
        except json.JSONDecodeError:
            ledger = {"total_live_calls": 0, "calls": []}
        ledger["total_live_calls"] += 1
        ledger["calls"].append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                "engine": params.get("engine"), "q": params.get("q")})
        path.write_text(json.dumps(ledger, indent=1, ensure_ascii=False))

    def search(self, params: dict, *, live: bool = True, retries: int = 2) -> dict:
        """Cached search. `live=False` returns {} on a cache miss instead of spending a credit."""
        params = {k: v for k, v in params.items() if v is not None}
        hit = self.cached(params)
        if hit is not None:
            self.cache_hits += 1
            return hit
        if not live or self.offline:
            return {}
        if not self.api_key:
            raise NoApiKey(f"SERPAPI_API_KEY is not set and this query is not cached: {params.get('engine')} "
                           f"{params.get('q', '')}")
        with self._lock:
            if self.max_credits is not None and self.live_calls - self.budget_start >= self.max_credits:
                raise CreditLimitReached(f"credit cap of {self.max_credits} live SerpApi calls reached "
                                         f"(SERPAPI_MAX_CREDITS)")
            self.live_calls += 1
        last_err: Exception | None = None
        what = f"{params.get('engine')} \"{str(params.get('q', ''))[:60]}\""
        for attempt in range(retries + 1):
            try:
                # (connect, read) timeouts: fail fast and retry rather than hang the UI for minutes.
                r = requests.get(ENDPOINT, params={**params, "api_key": self.api_key}, timeout=(5, 25))
                data = r.json()
                if r.status_code == 429 or r.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {r.status_code}: {data.get('error', '')}")
                break
            except (requests.RequestException, ValueError) as e:
                last_err = e
                if attempt < retries:
                    time.sleep(2 * (attempt + 1))
        else:
            self._refund()
            raise SerpApiError(f"SerpApi unreachable for {what} after {retries + 1} attempts: "
                               f"{type(last_err).__name__}: {str(last_err)[:120]}")
        if "error" in data and "hasn't returned any results" not in data["error"]:
            self._refund()
            raise SerpApiError(f"SerpApi error for {what}: {data['error']}")
        self._path(params).write_text(json.dumps({"params": params, "response": data}, ensure_ascii=False))
        with self._lock:
            self._ledger(params)
        self._tl.n = getattr(self._tl, "n", 0) + 1
        if self.verbose:
            print(f"  [serpapi] live #{self.live_calls} {params['engine']}: {str(params.get('q', ''))[:70]}",
                  file=sys.stderr)
        return data

    def thread_calls_reset(self) -> None:
        self._tl.n = 0

    def thread_calls(self) -> int:
        return getattr(self._tl, "n", 0)

    def _refund(self) -> None:
        """A failed request isn't billed by SerpApi, so it shouldn't use up the run's credit budget."""
        with self._lock:
            self.live_calls -= 1

    def new_budget(self, max_credits: int | None = None) -> None:
        """Start a fresh credit budget (called per complaint and per eval run)."""
        self.budget_start = self.live_calls
        if max_credits is not None:
            self.max_credits = max_credits

    def summary(self) -> str:
        cap = f" (cap {self.max_credits} per run)" if self.max_credits is not None else ""
        return f"SerpApi: {self.live_calls} live call(s){cap}, {self.cache_hits} from cache"


serp = SerpClient()
