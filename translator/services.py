"""Translation engine for WordBridge.

Why this exists
---------------
The old code detected ONE language for the whole text and sent everything to
MyMemory (500-character limit, weak quality). Text that mixes languages, e.g.
"Hello how are you ... ఎక్కడ ఉన్నావు", was therefore sent as English and the
Telugu part came back untouched.

What it does now
----------------
1. Splits the text into runs by writing system (Latin, Telugu, Devanagari...).
2. Translates each run separately, using the script to know the source
   language when it is unambiguous (Telugu script -> Telugu, etc.).
3. Uses Google Translate (via deep-translator) first, and falls back to
   MyMemory in small chunks if Google fails.
4. Reports which languages were found, so the UI can show them.
"""

import logging
import os
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import requests
from deep_translator import GoogleTranslator, MyMemoryTranslator
from langdetect import DetectorFactory, LangDetectException, detect

log = logging.getLogger("translator")

DetectorFactory.seed = 0  # make language detection deterministic

MAX_CHARS = 5000

# code: Google code | name | native label | speech-recognition locale | MyMemory code
LANGUAGES = [
    {"code": "en", "name": "English",   "native": "English",   "speech": "en-IN", "mm": "en-GB"},
    {"code": "hi", "name": "Hindi",     "native": "हिंदी",      "speech": "hi-IN", "mm": "hi-IN"},
    {"code": "te", "name": "Telugu",    "native": "తెలుగు",     "speech": "te-IN", "mm": "te-IN"},
    {"code": "ta", "name": "Tamil",     "native": "தமிழ்",      "speech": "ta-IN", "mm": "ta-IN"},
    {"code": "kn", "name": "Kannada",   "native": "ಕನ್ನಡ",      "speech": "kn-IN", "mm": "kn-IN"},
    {"code": "ml", "name": "Malayalam", "native": "മലയാളം",    "speech": "ml-IN", "mm": "ml-IN"},
    {"code": "bn", "name": "Bengali",   "native": "বাংলা",      "speech": "bn-IN", "mm": "bn-IN"},
    {"code": "mr", "name": "Marathi",   "native": "मराठी",      "speech": "mr-IN", "mm": "mr-IN"},
    {"code": "gu", "name": "Gujarati",  "native": "ગુજરાતી",    "speech": "gu-IN", "mm": "gu-IN"},
    {"code": "ur", "name": "Urdu",      "native": "اردو",       "speech": "ur-IN", "mm": "ur-PK"},
    {"code": "es", "name": "Spanish",   "native": "Español",   "speech": "es-ES", "mm": "es-ES"},
    {"code": "fr", "name": "French",    "native": "Français",  "speech": "fr-FR", "mm": "fr-FR"},
    {"code": "de", "name": "German",    "native": "Deutsch",   "speech": "de-DE", "mm": "de-DE"},
    {"code": "ja", "name": "Japanese",  "native": "日本語",      "speech": "ja-JP", "mm": "ja-JP"},
]
LANG_BY_CODE = {lang["code"]: lang for lang in LANGUAGES}

# Writing system -> language, only where it is unambiguous.
SCRIPT_TO_LANG = {
    "TELUGU": "te",
    "TAMIL": "ta",
    "KANNADA": "kn",
    "MALAYALAM": "ml",
    "BENGALI": "bn",
    "GUJARATI": "gu",
    "CJK": "ja",
}
# Writing systems shared by several supported languages.
SCRIPT_LANGS = {
    "LATIN": {"en", "es", "fr", "de"},
    "DEVANAGARI": {"hi", "mr"},
    "ARABIC": {"ur"},
}


class TranslationError(Exception):
    """Raised with a message that is safe to show to the user."""


# --------------------------------------------------------------------------
# Segmentation
# --------------------------------------------------------------------------
def _script_of(ch):
    """Writing system of a letter, or None for spaces, digits, punctuation, marks."""
    if not ch.isalpha():
        return None
    name = unicodedata.name(ch, "")
    first = name.split(" ")[0]
    if first in ("HIRAGANA", "KATAKANA", "CJK"):
        return "CJK"
    return first or None


def split_by_script(text):
    """Split text into [(script, run_text)], keeping every character.

    Neutral characters (spaces, punctuation, digits, vowel signs) stay with
    the run they appear in, so no text is ever lost.
    """
    runs = []
    current_script = None
    buffer = []
    pending_neutral = []  # neutral characters seen before the first letter

    for ch in text:
        script = _script_of(ch)
        if script is None:
            (buffer if current_script else pending_neutral).append(ch)
            continue
        if current_script is None:
            current_script = script
            buffer = pending_neutral + [ch]
            pending_neutral = []
        elif script == current_script:
            buffer.append(ch)
        else:
            runs.append((current_script, "".join(buffer)))
            current_script = script
            buffer = [ch]

    if current_script is None:
        return []  # no letters at all (digits / punctuation only)
    runs.append((current_script, "".join(buffer)))
    return runs


def _word_count(text):
    return len(re.findall(r"\w+", text))


def merge_inline_switches(runs):
    """Absorb a single foreign word sitting inside a sentence of another script.

    "ఈ app చాలా బాగుంది" should be translated as one sentence, not three
    fragments. Runs at the very start or end are never merged.
    """
    merged = []
    i = 0
    while i < len(runs):
        script, chunk = runs[i]
        is_inner = 0 < i < len(runs) - 1
        if (
            is_inner
            and _word_count(chunk) == 1
            and runs[i - 1][0] == runs[i + 1][0]
            and merged
        ):
            prev_script, prev_chunk = merged.pop()
            nxt_chunk = runs[i + 1][1]
            merged.append((prev_script, prev_chunk + chunk + nxt_chunk))
            i += 2
            continue
        if merged and merged[-1][0] == script:
            merged[-1] = (script, merged[-1][1] + chunk)
        else:
            merged.append((script, chunk))
        i += 1
    return merged


def pick_source(script, user_source):
    """Choose the source-language code for one run ('auto' if unsure)."""
    if script in SCRIPT_TO_LANG:
        return SCRIPT_TO_LANG[script]
    allowed = SCRIPT_LANGS.get(script, set())
    if user_source != "auto" and user_source in allowed:
        return user_source
    if len(allowed) == 1:
        return next(iter(allowed))
    return "auto"


# --------------------------------------------------------------------------
# Translation backends
# --------------------------------------------------------------------------
def _chunks(text, limit):
    """Split text into pieces of at most `limit` chars, preferring sentence ends."""
    pieces, current = [], ""
    for sentence in re.split(r"(?<=[.!?।॥。？！\n])\s*", text):
        if not sentence:
            continue
        while len(sentence) > limit:  # a single very long sentence
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            pieces.append(sentence[:cut])
            sentence = sentence[cut:].lstrip()
        if len(current) + len(sentence) + 1 > limit and current:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


def _google_cloud(text, source, target):
    """Official Google Cloud Translation API (needs GOOGLE_TRANSLATE_API_KEY).

    Most reliable option on a hosted server: the free web endpoints often
    block shared cloud IP addresses.
    """
    key = os.environ.get("GOOGLE_TRANSLATE_API_KEY")
    if not key:
        raise TranslationError("GOOGLE_TRANSLATE_API_KEY is not set.")
    payload = {"q": text, "target": target, "format": "text"}
    if source != "auto":
        payload["source"] = source
    response = requests.post(
        "https://translation.googleapis.com/language/translate/v2",
        params={"key": key}, data=payload, timeout=15,
    )
    response.raise_for_status()
    return response.json()["data"]["translations"][0]["translatedText"]


def _google(text, source, target):
    out = GoogleTranslator(source=source, target=target).translate(text)
    if not out:
        raise TranslationError("Empty result from Google Translate.")
    return out


def _mymemory(text, source, target):
    # MyMemory cannot auto-detect, so guess from the text itself.
    if source == "auto":
        try:
            source = detect(text)
        except LangDetectException:
            source = "en"
    if source not in LANG_BY_CODE:
        source = "en"
    # Giving an email raises MyMemory's free daily limit (set MYMEMORY_EMAIL).
    translator = MyMemoryTranslator(
        source=LANG_BY_CODE[source]["mm"], target=LANG_BY_CODE[target]["mm"],
        email=os.environ.get("MYMEMORY_EMAIL"),
    )
    parts = [translator.translate(piece) for piece in _chunks(text, 450)]
    parts = [p for p in parts if p]
    if not parts:
        raise TranslationError("Empty result from MyMemory.")
    return " ".join(parts)


def translate_chunk(text, source, target):
    """Translate one run, trying each backend in turn and logging failures."""
    if source == target:
        return text
    backends = []
    if os.environ.get("GOOGLE_TRANSLATE_API_KEY"):
        backends.append(("google-cloud", _google_cloud))
    backends += [("google-free", _google), ("mymemory", _mymemory)]

    last_error = None
    for name, backend in backends:
        try:
            return backend(text, source, target)
        except Exception as exc:  # noqa: BLE001 - we want to try the next backend
            last_error = exc
            # This line shows up in the Render logs and tells you what went wrong.
            log.warning("Translation backend %s failed: %s: %s", name, type(exc).__name__, exc)
    raise last_error


def _translate_run(args):
    script, chunk, source, target = args
    lead = chunk[: len(chunk) - len(chunk.lstrip())]
    trail = chunk[len(chunk.rstrip()):]
    core = chunk.strip()
    if not core or not any(c.isalpha() for c in core):
        return chunk
    return f"{lead}{translate_chunk(core, source, target)}{trail}"


def _label(script, source, chunk):
    """Human-readable language name for a run, for the 'Detected' note."""
    if source in LANG_BY_CODE:
        return LANG_BY_CODE[source]["name"]
    try:
        code = detect(chunk)
        if code in LANG_BY_CODE:
            return LANG_BY_CODE[code]["name"]
    except LangDetectException:
        pass
    return None


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def translate_text(text, source, target):
    """Translate `text` into `target`.

    source: a language code from LANGUAGES, or "auto".
    Returns {"translated": str, "detected": [language names]}.
    Raises TranslationError with a user-friendly message on failure.
    """
    text = (text or "").strip()
    if not text:
        raise TranslationError("Type, paste or speak something to translate.")
    if len(text) > MAX_CHARS:
        raise TranslationError("Please keep your text under 5,000 characters.")
    if target not in LANG_BY_CODE:
        raise TranslationError("Please choose a language to translate to.")
    if source != "auto" and source not in LANG_BY_CODE:
        raise TranslationError("Please choose a valid language to translate from.")
    if source == target:
        raise TranslationError("'From' and 'To' are the same language. Choose a different one.")

    runs = merge_inline_switches(split_by_script(text))
    if not runs:
        return {"translated": text, "detected": []}

    jobs = [(script, chunk, pick_source(script, source), target) for script, chunk in runs]

    detected = []
    for (script, chunk, src, _t) in jobs:
        name = _label(script, src, chunk)
        if name and name not in detected:
            detected.append(name)

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(_translate_run, jobs))
    except Exception as exc:
        log.error("All translation backends failed: %s: %s", type(exc).__name__, exc)
        busy = any(k in f"{type(exc).__name__} {exc}" for k in ("429", "TooManyRequests", "quota", "limit"))
        raise TranslationError(
            "The translation service is busy right now. Please try again in a minute."
            if busy else
            "Translation couldn't be completed. Please try again in a moment."
        ) from exc

    translated = "".join(results).strip()
    if not translated:
        raise TranslationError("The translation service returned an empty result.")
    return {"translated": translated, "detected": detected}
