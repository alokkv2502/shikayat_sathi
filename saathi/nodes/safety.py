"""Safety: the first node, and the one outcome that stops the pipeline.

Tiers
    emergency_now  violence happening now, an immediate threat to life, a medical emergency, self-harm risk
                   -> emergency_response: steps and contacts only, then END. No questions, no form, no letter.
    urgent         a serious threat that is not happening this minute (threats to kill, past domestic violence,
                   sexual violence, a missing person) -> the urgent steps first, then the normal (police) flow
    normal         -> the normal flow

The result is typed flags (violence_ongoing, threat_to_life, children_at_risk, self_harm, domestic_violence,
sexual_violence, missing_person, medical); the screen and the contacts are built from them in code - 1098 only
when children are at risk, 14416 only when self-harm is mentioned.

Rules run first (instant; also used by the app the moment you submit) and are a backstop: they can force
emergency_now or urgent, and the LLM's assessment (free with intake's single call) can raise the tier or add a flag
but never lower or remove one. These are India's national numbers (112 ERSS, 108, Childline 1098, Women Helpline
181, Tele-MANAS 14416, NALSA legal aid 15100, cyber-crime 1930); re-check them on the official portals before a
release."""
from __future__ import annotations

import re

from saathi.nodes.common import node

TIER_RANK = {"normal": 0, "urgent": 1, "emergency_now": 2}
FLAGS = ["violence_ongoing", "threat_to_life", "children_at_risk", "self_harm", "domestic_violence",
         "sexual_violence", "missing_person", "medical"]

CONTACTS = {
    "112": "Police / emergency — call now",
    "181": "Women Helpline",
    "1098": "Childline",
    "14416": "Tele-MANAS — free, confidential mental-health support, 24×7",
    "108": "Ambulance",
}

_P = {
    "violence_ongoing": r"\b(?:is|are|keeps?|started|been)\b[^.]{0,25}\b(?:beating|hitting|attacking|choking|"
                        r"strangling)\b|locked (?:myself|me|us)\b[^.]{0,50}\bin\b|hiding (?:from him|in the)|"
                        r"maar (?:raha|rahe|rahi) (?:hai|hain|he)|pit (?:raha|rahe|rahi)|पीट (?:रहा|रहे|रही)|"
                        r"मार (?:रहा|रहे|रही)|darwaza tod|दरवाज़ा तोड़|दरवाजा तोड़|break(?:ing)? (?:down )?(?:the|my) door|"
                        r"trying to (?:kill|break in|get in)|locked (?:the kids|my (?:kids|children))\b[^.]{0,30}\bin\b|"
                        r"bachao|बचाओ|help me (?:now|please)|he is here now|outside (?:my|the) door",
    "threat_to_life": r"threaten(?:ed|ing|s)? to kill|will kill (?:me|us|my)|kill me|going to kill|jaan se maar|"
                      r"जान से मार|\b(?:knife|gun|pistol|weapon)s?\b|acid attack|throw acid|hatya|हत्या",
    "domestic_violence": r"domestic violence|maarpeet|mar[- ]?peet|मारपीट|(?:husband|wife|in-?laws?|pati|sasural|"
                         r"ex[- ]?husband|partner|father)\b[^.]{0,60}(?:beat|hit|slap|maar|abuse|threat|kill)|"
                         # Hindi: no \b - Python does not treat a word ending in a vowel sign (पति) as a word end
                         r"(?:पति|ससुराल|सास|ससुर)[^।.]{0,60}(?:मार|पीट|धमकी)|(?:beat|hit|slap)s? me",
    "children_at_risk": r"(?:my |the )?(?:kids|children|child|baby|son|daughter|bachch?e|बच्चे|बच्चों|बेटा|बेटी)"
                        r"[^.।]{0,60}(?:locked|hiding|scared|beat|hit|hurt|with me|inside|andar|अंदर|बंद|साथ|danger|"
                        r"missing|kidnap|lapata|laapata|लापता|गुमशुदा|अपहरण)|"
                        r"(?:locked|hiding|beat|hit)[^.]{0,40}(?:kids|children|child|baby|bachch?e|बच्चे)",
    "self_harm": r"suicid|kill myself|end (?:my|this) life|want to die|don'?t want to live|can'?t go on|self[- ]?harm|"
                 r"cut myself|jaan de (?:dunga|dungi|denge)|khudkushi|marna chaht[aie]|आत्महत्या|ख़ुदकुशी|"
                 r"खुदकुशी|जान दे (?:दूंगा|दूंगी)|मरना चाहत",
    "sexual_violence": r"\brape|sexual(?:ly)? (?:assault|abuse)|molest|बलात्कार|यौन (?:शोषण|उत्पीड़न)|छेड़छाड़",
    "missing_person": r"missing (?:child|son|daughter|girl|boy|person|since)|(?:child|son|daughter|kid) (?:is|has gone|"
                      r"went) missing|kidnap|abduct|lapata|laapata|ghar (?:se )?nahi (?:aaya|aayi|lauta|lauti)|लापता|"
                      r"अपहरण|गुमशुदा|घर नहीं (?:आया|आई|लौटा|लौटी)",
    "medical": r"unconscious|not breathing|heart attack|heavy bleeding|bleeding heavily|overdose|took poison|behosh|"
               r"बेहोश|ज़हर खा",
}
_RES = {k: re.compile(v, re.I) for k, v in _P.items()}
NOW = re.compile(r"\b(?:right now|now|abhi|at this moment|currently|tonight)\b|अभी|इस समय", re.I)


def tier_of_flags(flags: dict, now: bool = False) -> str:
    if flags.get("violence_ongoing") or flags.get("medical") or flags.get("self_harm") \
            or (flags.get("threat_to_life") and now):
        return "emergency_now"
    if any(flags.get(k) for k in ("threat_to_life", "domestic_violence", "sexual_violence", "missing_person",
                                  "children_at_risk")):
        return "urgent"
    return "normal"


def contacts_for(flags: dict, tier: str) -> list[tuple[str, str]]:
    """The numbers this situation needs - built from the flags, nothing generic."""
    if tier == "normal":
        return []
    nums = []
    if flags.get("self_harm") and not (flags.get("violence_ongoing") or flags.get("threat_to_life")):
        nums.append("14416")  # self-harm alone: the mental-health line first
    nums.append("112")
    if flags.get("domestic_violence") or flags.get("sexual_violence"):
        nums.append("181")
    if flags.get("children_at_risk"):  # includes a missing or kidnapped child
        nums.append("1098")
    if flags.get("self_harm"):
        nums.append("14416")
    if flags.get("medical"):
        nums.append("108")
    return [(n, CONTACTS[n]) for n in dict.fromkeys(nums)]


def assess(flags: dict, *, now: bool, method: str, floor: str = "normal") -> dict:
    tier = max(tier_of_flags(flags, now), floor, key=TIER_RANK.get)
    contacts = contacts_for(flags, tier)
    return {"tier": tier, "flags": {k: bool(flags.get(k)) for k in FLAGS}, "now": now, "method": method,
            "contacts": contacts, "numbers": [n for n, _ in contacts],
            # older readers: level / helplines / categories
            "level": {"emergency_now": "emergency", "urgent": "urgent"}.get(tier, "none"),
            "helplines": [f"{n} — {label}" for n, label in contacts],
            "categories": [k for k in FLAGS if flags.get(k)], "messages": []}


def safety_check(text: str) -> dict:
    """Deterministic and instant: the keyword backstop."""
    flags = {k: bool(rx.search(text or "")) for k, rx in _RES.items()}
    return assess(flags, now=bool(NOW.search(text or "")), method="rules")


def merge_llm(rules: dict, llm: dict | None, label: str) -> dict:
    """The LLM's assessment (intake's single call) can raise the tier and add flags; never lower or remove one."""
    if not llm:
        return rules
    flags = {k: bool((rules.get("flags") or {}).get(k) or llm.get(k)) for k in FLAGS}
    now = bool(rules.get("now") or llm.get("violence_ongoing"))
    floor = max(rules.get("tier", "normal"), llm.get("tier") or "normal", key=TIER_RANK.get)
    res = assess(flags, now=now, method=rules.get("method", "rules"), floor=floor)
    if res["tier"] != rules.get("tier") or res["flags"] != rules.get("flags"):
        res["method"] = f"rules + {label}"
    return res


def screen(s: dict) -> dict:
    """The emergency screen, built in code from the flags: what to do now, who to call, what comes after."""
    f = s.get("flags") or {}
    if f.get("self_harm") and not (f.get("violence_ongoing") or f.get("threat_to_life")):
        headline = ("You don't have to go through this alone. Call Tele-MANAS on 14416 now — it is free and "
                    "confidential. If you are in immediate danger, call 112.")
    elif f.get("medical"):
        headline = "This sounds like a medical emergency. Call 108 or 112 now."
    else:
        headline = "This sounds like an emergency. Call 112 now."
    steps = []
    if f.get("violence_ongoing") or f.get("threat_to_life"):
        steps += ["Stay in a locked room if it's safe. Keep your phone with you.",
                  "If you can't talk, dial 112 and stay on the line, or press SOS in the 112 India app."]
    if f.get("children_at_risk"):
        steps.append("Keep the children with you if you can; Childline 1098 can also send help for them.")
    if f.get("missing_person"):
        steps.append("Report a missing person to the police on 112 right away — there is no waiting period.")
    if f.get("medical"):
        steps.append("Stay with the person; the ambulance operator will tell you what to do until help arrives.")
    after = []
    if f.get("domestic_violence"):
        after.append("You can get protection, residence and other orders under the Protection of Women from "
                     "Domestic Violence Act, 2005, through a Protection Officer or the Magistrate.")
    if f.get("domestic_violence") or f.get("sexual_violence") or f.get("violence_ongoing") or f.get("threat_to_life"):
        after.append("Free legal help: your District Legal Services Authority (DLSA), or call 15100.")
    return {"tier": s.get("tier"), "headline": headline, "steps": steps, "contacts": s.get("contacts") or [],
            "after": after,
            "offer": "When you're ready, I can help you write a police complaint."
            if not (f.get("self_harm") and not f.get("violence_ongoing")) else
            "When you're ready, I can help you with the complaint you came here for."}


def trace_line(res: dict) -> str:
    flags = ", ".join(k for k, v in (res.get("flags") or {}).items() if v)
    if res.get("tier") == "normal":
        return "🛟 Safety: no immediate danger detected"
    return (f"🛟 Safety: {res['tier'].upper()} — {flags} → " + ", ".join(res["numbers"]) + f" ({res['method']})")


@node("🛟 Safety")
def safety_router(state: dict) -> dict:
    """First node, rules only (instant). The LLM's judgement arrives with intake's single call (merge_llm)."""
    res = safety_check(state["complaint"])
    return {"safety": res, "trace": [trace_line(res)]}


def emergencies_end(state: dict) -> bool:
    """An emergency happening now ends the run at the emergency screen - unless the person, now safe, chose
    'help me write a complaint' (safety_acknowledged), in which case the contacts stay on top of the normal flow."""
    return (state.get("safety") or {}).get("tier") == "emergency_now" and not state.get("safety_acknowledged")


def route_after_safety(state: dict) -> str:
    return "emergency_response" if emergencies_end(state) else "intake_planner"


@node("🆘 Emergency")
def emergency_response(state: dict) -> dict:
    """Steps and contacts only - no interrupt, no questions, no drafting. Then END."""
    s = screen(state.get("safety") or {})
    return {"emergency": s, "trace": [f"🆘 Emergency screen: {s['headline']} Contacts: "
                                      + ", ".join(n for n, _ in s["contacts"]) + " — the run ends here"]}
