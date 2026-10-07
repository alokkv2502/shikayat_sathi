"""Page fetching (cached, short timeouts, size-limited, never raises) and contact-text helpers.
Fetched content is only parsed as text - never executed."""
from __future__ import annotations

import hashlib
import json
import re

import requests

from config import CACHE_DIR

WEB_CACHE = CACHE_DIR / "web"
WEB_CACHE.mkdir(parents=True, exist_ok=True)
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"

def deobfuscate(text: str) -> str:
    """'grievance [at] example [dot] com' -> 'grievance@example.com' (common on Indian grievance pages)."""
    t = re.sub(r"\s*[\[\(\{]\s*at\s*[\]\)\}]\s*", "@", text, flags=re.I)
    t = re.sub(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", ".", t, flags=re.I)
    return t


def fetch(url: str, *, max_bytes: int = 600_000) -> dict:
    """{url, final_url, ok, status, title, text, mailtos, tels, error}. Cached; never raises."""
    if not re.match(r"^https?://", url or ""):
        url = "https://" + (url or "")
    cache = WEB_CACHE / (hashlib.sha1(url.encode()).hexdigest() + ".json")
    if cache.exists():
        return json.loads(cache.read_text())
    out: dict = {"url": url, "final_url": url, "ok": False, "text": "", "mailtos": [], "tels": []}
    try:
        from bs4 import BeautifulSoup

        r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9"},
                         timeout=(5, 10), stream=True, allow_redirects=True)
        raw = r.raw.read(max_bytes * 4 if "pdf" in r.headers.get("content-type", "") else max_bytes,
                         decode_content=True)
        out.update(status=r.status_code, final_url=r.url)
        if raw[:5] == b"%PDF-":  # grievance policies are often published as PDFs
            import io

            from pypdf import PdfReader
            text = "\n".join(pg.extract_text() or "" for pg in PdfReader(io.BytesIO(raw)).pages[:40])
            text = deobfuscate(re.sub(r"[ \t\xa0]+", " ", text))
            out.update(ok=r.ok and len(text) > 80, text=text[:60000], title="PDF document",
                       mailtos=[], tels=[])
            cache.write_text(json.dumps(out, ensure_ascii=False))
            return out
        soup = BeautifulSoup(raw, "html.parser")
        out["mailtos"] = sorted({a["href"][7:].split("?")[0].strip() for a in soup.select("a[href^='mailto:']")})
        out["tels"] = sorted({a["href"][4:].strip() for a in soup.select("a[href^='tel:']")})
        for t in soup(["script", "style", "noscript", "svg"]):
            t.decompose()
        text = deobfuscate(re.sub(r"[ \t\xa0]+", " ", soup.get_text("\n")))
        text = re.sub(r"\n\s*\n+", "\n", text).strip()
        out.update(ok=r.ok and len(text) > 80, text=text[:60000],
                   title=(soup.title.string or "").strip() if soup.title and soup.title.string else "")
        if not r.ok:
            out["error"] = f"HTTP {r.status_code}"
    except Exception as e:  # DNS, TLS, timeout, decoding...
        out["error"] = f"{type(e).__name__}: {str(e)[:160]}"
    cache.write_text(json.dumps(out, ensure_ascii=False))
    return out


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# Indian phone formats: +91 / 0 prefixes, 10-digit mobiles, landlines with STD code, 1800/1860 toll-free.
PHONE_RE = re.compile(r"(?<![\d])(?:\+?91[\s-]?|0)?(?:1800|1860)[\s-]?\d{3}[\s-]?\d{3,4}(?![\d])|"
                      r"(?<![\d])(?:\+?91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?![\d])|"
                      r"(?<![\d])(?:\+?91[\s-]?|0)\d{2,4}[\s-]?\d{6,8}(?![\d])|"
                      r"(?<![\d])(?:1915|14448|155255|198|1930)(?![\d])")
FREE_MAIL = {"gmail.com", "yahoo.com", "yahoo.co.in", "hotmail.com", "outlook.com", "rediffmail.com", "live.com",
             "protonmail.com", "icloud.com", "aol.com", "ymail.com", "proton.me"}


def norm_email(e: str) -> str:
    return (e or "").strip().strip(".").lower()


def norm_phone(p: str) -> str:
    """Canonical digits: drops +91 / leading 0 so the same number matches across formats."""
    d = re.sub(r"\D", "", p or "")
    if d.startswith("91") and len(d) == 12:
        d = d[2:]
    if d.startswith("0") and len(d) == 11:
        d = d[1:]
    return d


def find_emails(text: str) -> list[str]:
    return sorted({norm_email(m) for m in EMAIL_RE.findall(text or "") if not m.lower().endswith((".png", ".jpg", ".gif", ".webp", ".svg"))})


def find_phones(text: str) -> list[str]:
    return sorted({norm_phone(m) for m in PHONE_RE.findall(text or "") if len(norm_phone(m)) >= 3})
