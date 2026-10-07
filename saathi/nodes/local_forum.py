"""local_forum worker: nearest District Consumer Disputes Redressal Commission (Google Maps)."""
from __future__ import annotations

import re

from saathi.nodes.common import live, node
from saathi.tools.serp import serp


@node("📍 Local forum")
def local_forum(state: dict) -> dict:
    city = state.get("city") or (state.get("facts") or {}).get("city")
    if not city:
        return {"local_forum": None, "trace": ["📍 Local forum: skipped — no city given"]}
    data = serp.search({"engine": "google_maps", "type": "search", "q": f"District Consumer Disputes Redressal "
                        f"Commission {city}", "gl": "in", "hl": "en"}, live=live(state))
    for r in data.get("local_results") or []:
        if re.search(r"consumer|forum|commission", r.get("title", ""), re.I):
            gps = r.get("gps_coordinates") or {}
            link = (f"https://www.google.com/maps/search/?api=1&query={gps['latitude']},{gps['longitude']}"
                    if gps.get("latitude") else None)
            forum = {"name": r.get("title"), "address": r.get("address"), "phone": r.get("phone"),
                     "website": r.get("website"), "maps_link": link, "city": city}
            return {"local_forum": forum, "trace": [f"📍 Local forum: {forum['name']}"]}
    return {"local_forum": None, "trace": [f"📍 Local forum: no consumer commission found for {city}"]}
