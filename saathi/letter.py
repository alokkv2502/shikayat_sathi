"""Letter layout in ONE language. The drafter writes the subject, the addressee lines and the body in the letter's
language; everything the code adds around them - salutation, date, sign-off, headings, footers - comes from here in
the same language, so a letter is never half English and half Hindi.

letter_language is "en" or "hi" (Devanagari). It defaults to Hindi for complaints written in Hindi and English
otherwise, and the complainant can change it in the details form."""
from __future__ import annotations

import re
from datetime import date

from saathi.deadlines import fmt

LANGS = {"en": "English", "hi": "Hindi (Devanagari script)"}
LABELS = {
    "en": {"to": "To,", "date": "Date", "subject": "Subject", "salutation": "Dear Sir/Madam,",
           "closing": "Yours faithfully,", "name": "[YOUR NAME]", "phone": "[PHONE]", "email_ph": "[EMAIL]",
           "address": "[ADDRESS]", "email": "Email", "gro": "The Grievance Redressal Officer",
           "sho": "The Station House Officer", "station": "{} Police Station",
           "basis": "Legal basis cited:", "sources": "Sources:", "details": "Details I can confirm about my case:",
           "receipt": "Keep a copy and ask for a signed, stamped receipt (or the FIR copy) when you submit this.",
           "not_advice": "This is general information to help you take the next step. It is not legal advice; a "
                         "lawyer or the District Legal Services Authority can advise on your situation."},
    "hi": {"to": "सेवा में,", "date": "दिनांक", "subject": "विषय", "salutation": "महोदय/महोदया,",
           "closing": "भवदीय,", "name": "[आपका नाम]", "phone": "[फ़ोन]", "email_ph": "[ईमेल]", "address": "[पता]",
           "email": "ईमेल", "gro": "शिकायत निवारण अधिकारी", "sho": "थाना प्रभारी (SHO)", "station": "थाना {}",
           "basis": "उद्धृत कानूनी आधार:", "sources": "स्रोत:", "details": "मेरे मामले से जुड़ी पुष्टि की गई जानकारी:",
           "receipt": "इसकी एक प्रति अपने पास रखें और जमा करते समय हस्ताक्षरित व मुहर लगी पावती (या FIR की प्रति) "
                      "अवश्य लें।",
           "not_advice": "यह आगे का कदम उठाने में मदद के लिए सामान्य जानकारी है, कानूनी सलाह नहीं। अपनी स्थिति पर सलाह के लिए "
                         "किसी वकील या ज़िला विधिक सेवा प्राधिकरण से संपर्क करें।"},
}
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_LATIN = re.compile(r"[A-Za-z]")


def default_language(complaint_language: str) -> str:
    return "hi" if complaint_language == "hi" else "en"


def labels(lang: str) -> dict:
    return LABELS.get(lang, LABELS["en"])


def today(lang: str) -> str:
    return fmt(date.today(), lang)


def _strip_allowed(text: str) -> str:
    """Remove what may stay in Latin script in a Hindi letter (and in any letter): [PLACEHOLDERS], [C#] tags,
    URLs, emails, and tokens with digits (IDs, amounts)."""
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"https?://\S+|\S+@\S+", " ", text)
    return re.sub(r"\S*\d\S*", " ", text)


def language_issue(text: str, lang: str, known_names: list[str] | None = None) -> str:
    """'' if the text is in one language, else what is wrong. Proper names the complainant used (company, scheme)
    may stay as written."""
    t = _strip_allowed(text)
    for n in known_names or []:
        if n:
            t = t.replace(n, " ")
    dev, lat = len(_DEVANAGARI.findall(t)), len(_LATIN.findall(t))
    if lang == "en" and dev:
        return "The letter must be entirely in English, but it contains Hindi (Devanagari) text."
    phrase = re.search(r"[A-Za-z]{2,}(?:[\s,()/-]+[A-Za-z]{2,}){1,}", t)  # an English phrase, not just a name
    if lang == "hi" and (phrase or lat > max(6, 0.04 * (dev + lat))):  # a few letters: abbreviations (ID, SHO, FIR)
        return ("The letter must be entirely in Hindi (Devanagari), but it mixes in English"
                + (f" (“{phrase.group(0).strip()}”)" if phrase else "") + " - write it in Hindi.")
    return ""
