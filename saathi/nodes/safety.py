"""safety_router: the first node. If the complaint suggests immediate danger (violence, a threat to life, a missing
child, suicide risk, sexual assault, a medical emergency) the emergency numbers are shown before anything else.
The complaint still continues through the pipeline - the person may also need the written complaint.

Rules run first (instant, also used by the UI before the graph starts) and are deliberately broad. The LLM's view
comes free with intake's single call (IntakeOut.danger): it can add a category the rules missed, never remove one. These are India's national helpline numbers
(112 ERSS, 108, Childline 1098, Women Helpline 181, Tele-MANAS 14416, cyber-crime 1930); re-check them on the official
portals before a release."""
from __future__ import annotations

import re

from saathi.nodes.common import node

HELPLINES = {
    "112": "112 — National emergency number (police, fire, ambulance)",
    "108": "108 — Ambulance (most states)",
    "1098": "1098 — Childline (children in danger or missing)",
    "181": "181 — Women Helpline",
    "14416": "14416 or 1-800-891-4416 — Tele-MANAS, free 24×7 mental-health support",
    "1930": "1930 — National Cyber Crime Helpline (report money lost to fraud immediately)",
}
CATEGORIES = {
    "self_harm": {
        "pattern": r"suicid|kill myself|end (?:my|this) life|want to die|don'?t want to live|self[- ]?harm|"
                   r"cut myself|jaan de (?:dunga|dungi|denge)|khudkushi|marna chaht[aie]|आत्महत्या|ख़ुदकुशी|खुदकुशी|"
                   r"जान दे (?:दूंगा|दूंगी)|मरना चाहत",
        "helplines": ["14416", "112"],
        "message": "If you are thinking about ending your life or hurting yourself, please call Tele-MANAS on 14416 "
                   "now — it is free and confidential — or 112 in an emergency. You don't have to go through this "
                   "alone.",
    },
    "violence": {
        "pattern": r"(?:is|are|keeps?|started) (?:beating|hitting|attacking)|beat(?:s|en)? me|hit(?:s|ting)? me|"
                   r"domestic violence|threaten(?:ed|ing|s)? to kill|will kill (?:me|us|my)|kill me|"
                   r"\b(?:knife|gun|pistol|weapon)s?\b|acid attack|throw acid|maar (?:raha|rahe|rahi|diya|dala|denge|dalenge)|maarpeet|mar[- ]?peet|"
                   r"jaan se maar|मारपीट|जान से मार|पीट (?:रहा|रहे|रही)|हत्या",
        "helplines": ["112", "181"],
        "message": "If you are in danger right now, call 112 (or 181 for women's safety). Get to a safe place first; "
                   "the complaint can wait.",
    },
    "sexual_violence": {
        "pattern": r"\brape|sexual(?:ly)? (?:assault|abuse)|molest|बलात्कार|यौन (?:शोषण|उत्पीड़न)|छेड़छाड़",
        "helplines": ["112", "181"],
        "message": "If you have been sexually assaulted or are at risk, call 112 or the Women Helpline 181. Police "
                   "must register your complaint, and every hospital must treat you free of charge.",
    },
    "missing_or_abducted": {
        "pattern": r"missing (?:child|son|daughter|girl|boy|person|since)|(?:child|son|daughter|kid) (?:is|has gone|went) "
                   r"missing|kidnap|abduct|lapata|laapata|ghar (?:se )?nahi (?:aaya|aayi|lauta|lauti)|लापता|अपहरण|"
                   r"गुमशुदा|घर नहीं (?:आया|आई|लौटा|लौटी)",
        "helplines": ["112", "1098"],
        "message": "Report a missing or abducted person at the nearest police station or on 112 immediately — there "
                   "is no waiting period. For a child, also call Childline 1098.",
    },
    "medical": {
        "pattern": r"unconscious|not breathing|heart attack|heavy bleeding|bleeding heavily|overdose|took poison|"
                   r"behosh|बेहोश|ज़हर खा",
        "helplines": ["108", "112"],
        "message": "For a medical emergency call 108 or 112 now.",
    },
}
_RES = {k: re.compile(v["pattern"], re.I) for k, v in CATEGORIES.items()}

def safety_check(text: str) -> dict:
    """Deterministic, instant: {level: emergency|none, categories, helplines: [text], messages: [text]}."""
    cats = [k for k, rx in _RES.items() if rx.search(text or "")]
    return _result(cats, "rules")


def _result(cats: list[str], method: str) -> dict:
    nums = list(dict.fromkeys(n for c in cats for n in CATEGORIES[c]["helplines"]))
    return {"level": "emergency" if cats else "none", "categories": cats, "method": method,
            "helplines": [HELPLINES[n] for n in nums], "numbers": nums,
            "messages": [CATEGORIES[c]["message"] for c in cats]}


def merge_llm(rules: dict, llm_categories: list[str], label: str) -> dict | None:
    """Intake's LLM call also judges danger (no separate call). Returns the updated result if it ADDS a category
    the rules missed (it can never remove one), else None."""
    extra = [c for c in llm_categories or [] if c in CATEGORIES and c not in (rules or {}).get("categories", [])]
    if not extra:
        return None
    return _result(list(dict.fromkeys((rules or {}).get("categories", []) + extra)), f"rules + {label}")


def trace_line(res: dict) -> str:
    return ("🛟 Safety: EMERGENCY — " + ", ".join(res["categories"]) + " → show " + ", ".join(res["numbers"])
            + f" ({res['method']})" if res["level"] == "emergency" else "🛟 Safety: no immediate danger detected")


@node("🛟 Safety")
def safety_router(state: dict) -> dict:
    """First node, rules only (instant). The LLM's judgement arrives with intake's single call (merge_llm)."""
    res = safety_check(state["complaint"])
    return {"safety": res, "trace": [trace_line(res)]}
