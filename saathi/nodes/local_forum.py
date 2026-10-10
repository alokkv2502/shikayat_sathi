"""local_forum worker: the local office that matters for this domain (Google Maps) - the District Consumer
Commission for consumer cases, the cyber-crime police station or SP office for police matters, the District Legal
Services Authority for court matters."""
from __future__ import annotations

import re

from saathi.nodes.common import live, node
from saathi.playbooks import forum_query
from saathi.tools.serp import serp


@node("📍 Local forum")
def local_forum(state: dict) -> dict:
    city = state.get("city") or (state.get("facts") or {}).get("city")
    if not city:
        return {"local_forum": None, "trace": ["📍 Local forum: skipped — no city given"]}
    q = forum_query(state.get("sector") or "other", city, "cyber_fraud" in (state.get("issue_types") or []))
    if not q:
        return {"local_forum": None, "trace": ["📍 Local forum: not needed for this domain"]}
    query, pattern, label = q
    data = serp.search({"engine": "google_maps", "type": "search", "q": query, "gl": "in", "hl": "en"},
                       live=live(state))
    for r in data.get("local_results") or []:
        if re.search(pattern, r.get("title", ""), re.I):
            gps = r.get("gps_coordinates") or {}
            link = (f"https://www.google.com/maps/search/?api=1&query={gps['latitude']},{gps['longitude']}"
                    if gps.get("latitude") else None)
            forum = {"name": r.get("title"), "address": r.get("address"), "phone": r.get("phone"),
                     "website": r.get("website"), "maps_link": link, "city": city, "kind": label}
            return {"local_forum": forum, "trace": [f"📍 Local forum ({label}): {forum['name']}"]}
    return {"local_forum": None, "trace": [f"📍 Local forum: no {label.lower()} found for {city}"]}
