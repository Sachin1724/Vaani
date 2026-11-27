"""
odia_mix.py — English words in English letters inside Odia dictation
=====================================================================
The Odia model writes everything in Odia script, so "business friendly" comes out as
"ବିଜନେସ ଫ୍ରେଣ୍ଡଲି". odia_english.tsv.gz (build_odia_english.py) maps the Odia spellings of
25k common English words, generated from their CMUdict pronunciations, back to English.

A word is switched only when its spelling matches and English is the likelier reading:
native-only letters, common Odia words and short words that are not English function words
stay in Odia script. ponytail: word-by-word lookup, no context; homophones take the more
frequent word ("no" for "know") and misheard English stays in Odia script.
"""

import gzip
import json
import os
import re
from pathlib import Path

VIRAMA = "୍"
CONS = set(chr(c) for c in range(0x0B15, 0x0B3A)) | {"ୟ", "ୱ"}
_MAP = str.maketrans({"ୀ": "ି", "ୂ": "ୁ", "ଈ": "ଇ", "ଊ": "ଉ", "ଶ": "ସ", "ଷ": "ସ", "ଣ": "ନ", "ଂ": "ନ", "ଙ": "ନ",
                      "ଵ": "ୱ", "ୈ": "େ", "ୌ": "ୋ", VIRAMA: None, "‌": None, "‍": None, "଼": None, "ଁ": None, "ଃ": None})
NATIVE = set("ଖଘଛଝଞଠଢଧଯଋୠଐଔ")  # letters Odia spellings of English words never use (nor କ୍ଷ)
SHORT = set("i a is it in on of to the so and but you we my me he she do go no ok or at as be by if up us am an "
            "all are was has had have not now how who why what when this that then than there they our your yes".split())
# Common Odia words that sound like English ones (ଫୁଲ flower, not "full"; ମୋର my, not "more")
ODIA = set("ଟି ଟା ଏ ମୋର କଣ ବନ ଆର ଦାନ କାର ଶୋ କଷ୍ଟ ଦି ଫୁଲ ବାଟ ମାନ ଦାମ ଦାମ୍ ପ୍ର ସାନ ବାଣ କେଶ ସନ ସନ୍ ପର ସେଲ୍ "
           "କର କରି ଦୋଷ ମାନି ସାପ ଷ୍ଟାର ଏସ୍ ଏସ ଏନ୍ ଏନ ଏମ୍ ଏମ ଓ ତ ଦଶ ଆଠ ବନାନ ମାପ ହାନ ମାଈ ଆଶ ଆଶ୍".split())
SUFFIXES = ("ର", "ରେ", "ରୁ", "କୁ", "ଟା", "ଟି")  # କାମ+ର "of work", not "camera"
OVERRIDE = {"ଅଫ": "of", "ଓକେ": "OK", "ହଭ": "have", "ଏମବିଏ": "MBA",
            # Brand and app names: not in the pronouncing dictionary the table is built from, but
            # among the English words people say most inside Odia sentences.
            "ୟୁଟ୍ୟୁବ": "YouTube", "ୟୁଟୁବ": "YouTube", "ୟୁଟ୍ୟୁବ୍": "YouTube",
            "ହ୍ୱାଟସଆପ": "WhatsApp", "ୱାଟସଆପ": "WhatsApp", "ହ୍ୱାଟସ୍ଆପ୍": "WhatsApp", "ୱାଟ୍ସଆପ୍": "WhatsApp",
            "ଫେସବୁକ": "Facebook", "ଇନଷ୍ଟାଗ୍ରାମ": "Instagram", "ଇନ୍ସଟାଗ୍ରାମ": "Instagram",
            "ଗୁଗଲ": "Google", "ଜିମେଲ": "Gmail", "ଜିମେଲ୍": "Gmail", "ଚାଟଜିପିଟି": "ChatGPT",
            # Common English words frequently emitted in Odia script by IndicConformer
            "ଦେୟର": "there", "ଇସ୍ୟୁଜ୍": "issues", "ଇସୁ": "issue", "ୱନଥିଙ୍ଗ": "one thing",
            "ମଡେଲ": "model", "ଟ୍ରେଣ୍ଡ": "trained", "କଷ୍ଟମଲି": "customly", "ଅଟୋ": "auto",
            "ମିକ୍ସଡ": "mixed", "ରାଣ୍ଡମାଇଜିଂ": "randomizing", "ଇଜି": "easy", "ସବ୍": "sub",
            "ସବଟାଇଟଲ": "subtitle", "ଆର": "are", "ପ୍ରୋଭାଇଡିଙ୍ଗ": "providing",
            "ଲିଟରାଲ୍ଯ୍": "literally", "ତବଲ": "table", "ଟବଲ": "table", "ଲିକ": "like",
            "ବ୍ୟାରି": "very", "ବେରି": "very", "ଭେରି": "very", "ଭ୍ୟାରି": "very", "ବକ୍": "back",
            # Numbers & multi-syllable English words frequently left in Odia script by IndicConformer
            "ଟ୍ବେଣ୍ଟି": "twenty", "ଟ୍ବେନ୍ଟି": "twenty", "ଥାର୍ଟି": "thirty", "ଫର୍ଟି": "forty",
            "ଫିଫ୍ଟି": "fifty", "ଥୋଉସଣ୍ଡ": "thousand", "ଥାଉଜାଣ୍ଡ": "thousand", "ହଣ୍ଡ୍ରେଡ": "hundred",
            "ବ୍ୟୁଟିଫୁଲି": "beautifully", "ରିପ୍ରେଜେଣ୍ଟସ": "represents", "ଟ୍ରିଡେ": "today",
            "ମାନିଫେଷ୍ଟ": "manifest", "ଗୋଇଙ୍ଗ": "going", "ଡାଇରେକ୍ଟିଂ": "directing", "କ୍ରିଏଟିଭିଟି": "creativity"}
MARGIN = 0.4  # keep English terms in English even when IndicConformer emits Odia phonetics
TABLE_PATH = os.path.join(os.path.dirname(__file__), "odia_english.tsv.gz")


def key(word: str) -> str:
    """Spelling with the variation Odia writers use for English sounds folded away."""
    return word.translate(_MAP)


_OVERRIDE = {key(k): v for k, v in OVERRIDE.items()}
_LEARNED_PATH = Path.home() / ".vaani" / "learned_odia_mix.json"
_learned = None
_table = None
_MATRA_STRIP = re.compile("[\u0B3E-\u0B4C\u0B4D\u0B01-\u0B03]")


def _load_learned() -> dict:
    global _learned
    if _learned is None:
        _learned = {}
        try:
            if _LEARNED_PATH.exists():
                data = json.loads(_LEARNED_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    _learned.update({key(k): str(v) for k, v in data.items() if k and v})
        except Exception:
            pass
    return _learned


def learn(odia_word: str, english_word: str) -> bool:
    """Persistently learn a user-corrected Odia-script -> English word mapping."""
    ow, ew = (odia_word or "").strip(), (english_word or "").strip()
    if not ow or not ew or not re.search("[\u0B00-\u0B7F]", ow) or not re.fullmatch(r"[A-Za-z0-9'.-]+", ew):
        return False
    learned = _load_learned()
    k = key(ow)
    sk = _MATRA_STRIP.sub("", k)
    changed = learned.get(k) != ew
    learned[k] = ew
    if len(sk) >= 2 and sk not in learned:
        learned[sk] = ew
    if changed:
        try:
            _LEARNED_PATH.parent.mkdir(parents=True, exist_ok=True)
            _LEARNED_PATH.write_text(json.dumps(learned, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
    return True


def _load() -> dict:
    global _table
    if _table is None:
        with gzip.open(TABLE_PATH, "rt", encoding="utf-8") as f:
            _table = {k: (w, float(lp)) for k, w, lp in (line.rstrip("\n").split("\t") for line in f)}
    return _table


def english(word: str, lm=None):
    """The English word an Odia-script word spells, or None to keep it in Odia script."""
    k = key(word)
    learned = _load_learned()
    if k in learned:
        return learned[k]
    sk = _MATRA_STRIP.sub("", k)
    if len(sk) >= 2 and sk in learned and not word in ODIA:
        return learned[sk]
    if k in _OVERRIDE:
        return _OVERRIDE[k]
    hit = _load().get(k)
    if not hit or word in ODIA or NATIVE & set(word) or "କ୍ଷ" in word:
        return None
    en, en_lp = hit
    if en not in SHORT and (len(en) < 3 or sum(c in CONS for c in word) < 2 or not set(en) & set("aeiouy")):
        return None
    if lm is not None:
        if any(word.endswith(s) and lm.words.get(word[:-len(s)], 1 << 30) < 5000 for s in SUFFIXES):
            return None
        or_lp = lm.score("", word) - lm.BACKOFF if word in lm.words else lm.unk
        if en_lp <= or_lp + MARGIN:
            return None
    return "I" if en == "i" else en


_PHONETIC_FIXES = (
    ("barry barry", "very very"),
    ("berry berry", "very very"),
    ("barry beautiful", "very beautiful"),
    ("berry beautiful", "very beautiful"),
    ("is buck", "is back"),
    ("come buck", "come back"),
    ("go buck", "go back"),
)

_UNITS = {
    "zero": 0, "oh": 0, "one": 1, "two": 2, "to": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}


def _spoken_numbers_to_digits(text: str) -> str:
    """Convert spoken English number/year sequences (e.g. 'two thousand twenty seven' -> '2027', 'five hundred' -> '500')."""
    words = text.split()
    out, i = [], 0
    while i < len(words):
        w0 = words[i].lower()
        # 1. <unit> thousand ... ('two thousand twenty seven' -> '2027')
        if i + 1 < len(words) and w0 in _UNITS and words[i + 1].lower() == "thousand":
            val = _UNITS[w0] * 1000
            j = i + 2
            while j < len(words):
                wj = words[j].lower()
                if wj in _TENS:
                    val += _TENS[wj]; j += 1
                elif wj in _UNITS:
                    val += _UNITS[wj]; j += 1
                elif wj == "hundred" and val % 1000 in _UNITS and val % 1000 > 0:
                    val = (val // 1000) * 1000 + (val % 1000) * 100; j += 1
                else:
                    break
            out.append(str(val))
            i = j
            continue
        # 2. <unit> hundred ... ('five hundred' -> '500')
        if i + 1 < len(words) and w0 in _UNITS and words[i + 1].lower() == "hundred":
            val = _UNITS[w0] * 100
            j = i + 2
            while j < len(words):
                wj = words[j].lower()
                if wj in _TENS:
                    val += _TENS[wj]; j += 1
                elif wj in _UNITS:
                    val += _UNITS[wj]; j += 1
                else:
                    break
            out.append(str(val))
            i = j
            continue
        # 3. Spoken year: 'twenty <tens> [unit]' ('twenty twenty nine' -> '2029')
        if i + 1 < len(words) and w0 == "twenty" and words[i + 1].lower() in _TENS:
            val = 2000 + _TENS[words[i + 1].lower()]
            j = i + 2
            if j < len(words) and words[j].lower() in _UNITS and words[j].lower() != "to":
                val += _UNITS[words[j].lower()]; j += 1
            out.append(str(val))
            i = j
            continue
        # 4. <twenty..ninety> <unit> -> 'twenty seven' -> '27'
        if i + 1 < len(words) and w0 in _TENS and words[i + 1].lower() in _UNITS and words[i + 1].lower() != "to":
            out.append(str(_TENS[w0] + _UNITS[words[i + 1].lower()]))
            i += 2
            continue
        out.append(words[i])
        i += 1
    return " ".join(out)


def to_mixed(text: str, lm=None) -> str:
    out = " ".join(english(w, lm) or w for w in text.split())
    low = out.lower()
    for bad, good in _PHONETIC_FIXES:
        if bad in low:
            idx = low.find(bad)
            while idx != -1:
                out = out[:idx] + good + out[idx + len(bad):]
                low = out.lower()
                idx = low.find(bad, idx + len(good))
    return _spoken_numbers_to_digits(out)


if __name__ == "__main__":
    out = to_mixed("ବିଜନେସ ଫ୍ରେଣ୍ଡଲି ନଥିଲା ଆଉ ମୋର ଫୁଲ ଅଫ ଦ ୱର୍ଲ୍ଡ")
    assert out == "business friendly ନଥିଲା ଆଉ ମୋର ଫୁଲ of the world", out
    print("ok")
