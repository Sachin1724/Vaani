import re
from typing import List, Dict, Any, Optional

from .refiner import (
    SpeechToWritingEngine,
    resolve_self_corrections,
    clean_fillers,
    format_tech_terms,
    format_currency_and_quantities,
    format_code_identifiers,
    SPOKEN_COMMANDS,
)
from .odia_refine import refine_odia


def expand_snippets(text: str, snippets: List[Dict[str, Any]]) -> str:
    """Replace spoken snippet triggers ("my address") with their saved text, longest trigger first.
    A dictation that is only the trigger (plus punctuation) becomes exactly the snippet."""
    for s in sorted(snippets, key=lambda e: -len(e.get("phrase_match", ""))):
        trigger, body = (s.get("phrase_match") or "").strip(), s.get("replace_with") or ""
        if not trigger:
            continue
        if re.fullmatch(r"\W*" + re.escape(trigger) + r"\W*", text, flags=re.I):
            return body
        text = re.sub(r"\b" + re.escape(trigger) + r"\b", lambda _m: body, text, flags=re.I)
    return text


def find_corrections(pasted: str, field_text: str) -> List[tuple]:
    """Single-word fixes the user made to pasted dictation: [(heard, corrected), ...].
    The pasted words are aligned against the field; only 1:1 swaps that look like spelling fixes
    (similar letters, not a rewrite) count, and nothing is learned if the text changed a lot."""
    import difflib
    words = lambda t: re.findall(r"[\w'+-]+", t)
    p, f = words(pasted), words(field_text)
    if len(p) < 3 or not f:
        return []
    # the stretch of the field that best matches what was pasted
    best, best_i = 0.0, 0
    for i in range(max(1, len(f) - len(p) + 1)):
        r = difflib.SequenceMatcher(None, [w.lower() for w in p], [w.lower() for w in f[i:i + len(p)]]).ratio()
        if r > best:
            best, best_i = r, i
    if best < 0.6:
        return []
    window = f[best_i:best_i + len(p) + 1]
    out = []
    for op, a1, a2, b1, b2 in difflib.SequenceMatcher(None, p, window).get_opcodes():
        if op == "replace" and a2 - a1 == 1 and b2 - b1 == 1:
            old, new = p[a1], window[b1]
            if old.lower() != new.lower() and difflib.SequenceMatcher(None, old.lower(), new.lower()).ratio() >= 0.5:
                out.append((old, new))
    return out


class TextFormatter:
    def __init__(self, dictionary_entries: Optional[List[Dict[str, Any]]] = None):
        self.dictionary = dictionary_entries or []
        self.engine = SpeechToWritingEngine()

    def set_dictionary(self, entries: List[Dict[str, Any]]):
        self.dictionary = entries

    def format(
        self,
        text: str,
        tone_preset: str = "smart_clean",
        language: str = "en",
        context_category: str = "general",
    ) -> str:
        if not text or not text.strip():
            return ""

        raw = text.strip()

        # Verbatim mode: only custom dictionary substitutions, no rewriting
        if tone_preset == "verbatim":
            return self._apply_dictionary(raw)

        # Odia has its own refinement pass: refiner.py's patterns are all Latin-script and
        # match nothing in Odia text, so without this the transcript reached the editor raw.
        if language in ("or", "odia", "or-en", "or-mix", "or-auto"):
            refined = refine_odia(raw, strip_fillers=(tone_preset != "verbatim"))
            return self._apply_dictionary(refined)

        # For languages other than English, Hinglish, and Indian English,
        # Whisper handles sentence boundaries; apply self-corrections and custom vocab
        if language not in ("en", "en-in", "hinglish", "auto"):
            refined = resolve_self_corrections(raw)
            return self._apply_dictionary(refined)

        # Full local Speech-to-Writing refinement pipeline
        refined = self.engine.refine(
            raw_text=raw,
            context_category=context_category,
            tone_preset=tone_preset,
            language=language,
            custom_dictionary=self.dictionary,
        )

        # Preset-specific layout structures
        if tone_preset == "email":
            refined = self._format_email(refined)
        elif tone_preset == "bullets":
            refined = self._format_bullets(refined)

        return refined

    def _apply_dictionary(self, text: str) -> str:
        result = text
        for entry in self.dictionary:
            phrase = entry.get("phrase_match", "")
            replacement = entry.get("replace_with", "")
            case_sens = bool(entry.get("case_sensitive", 0))

            if not phrase or not replacement:
                continue

            flags = 0 if case_sens else re.IGNORECASE
            pattern = r"\b" + re.escape(phrase) + r"\b"
            result = re.sub(pattern, replacement, result, flags=flags)
        return result

    def _format_smart_clean(self, text: str) -> str:
        if not text:
            return ""

        # Sentence capitalization
        sentences = re.split(r"([.?!]\s+|\n+)", text)
        result = []
        for s in sentences:
            if s and s[0].isalpha():
                s = s[0].upper() + s[1:]
            result.append(s)
        formatted = "".join(result)

        if formatted and formatted[-1].isalnum():
            formatted += "."

        return formatted

    def _format_developer(self, text: str) -> str:
        s = text

        # Syntax substitutions
        substitutions = [
            (r"\bopen parenthesis\b", "("),
            (r"\bclose parenthesis\b", ")"),
            (r"\bopen bracket\b", "["),
            (r"\bclose bracket\b", "]"),
            (r"\bopen brace\b", "{"),
            (r"\bclose brace\b", "}"),
            (r"\bdot ([a-zA-Z0-9_]+)\b", r".\1"),
            (r"\barrows\b|\bfat arrow\b", "=>"),
            (r"\bthin arrow\b", "->"),
            (r"\bequals equals\b|\bdouble equals\b", "=="),
            (r"\btriple equals\b", "==="),
            (r"\bnot equals\b", "!="),
            (r"\bplus equals\b", "+="),
            (r"\bminus equals\b", "-="),
        ]
        for pat, repl in substitutions:
            s = re.sub(pat, repl, s, flags=re.IGNORECASE)

        # "camel case word1 word2" -> word1Word2
        def _to_camel(m):
            words = m.group(1).split()
            if not words:
                return ""
            return words[0].lower() + "".join(w.capitalize() for w in words[1:])

        s = re.sub(r"\bcamel case ([a-zA-Z0-9]+(?:\s+[a-zA-Z0-9]+){1,4})", _to_camel, s, flags=re.IGNORECASE)

        # "snake case word1 word2" -> word1_word2
        def _to_snake(m):
            words = m.group(1).split()
            return "_".join(w.lower() for w in words)

        s = re.sub(r"\bsnake case ([a-zA-Z0-9]+(?:\s+[a-zA-Z0-9]+){1,4})", _to_snake, s, flags=re.IGNORECASE)

        out = self._format_smart_clean(s)
        # Don't capitalize a leading identifier like userName / user_name
        if re.match(r"^[A-Z][a-z0-9]*(?:[A-Z_]\w*)\.?$", out) and s[:1].islower():
            out = out[0].lower() + out[1:]
        return out

    def _format_email(self, text: str) -> str:
        s = self._format_smart_clean(text)

        # Detect greeting
        greeting_match = re.match(r"^(Hi|Hello|Hey|Dear)\s+([A-Z][a-z]+)[,.]?\s*", s, flags=re.IGNORECASE)
        if greeting_match:
            greeting = f"{greeting_match.group(1).capitalize()} {greeting_match.group(2)},\n\n"
            body = s[greeting_match.end():]
        else:
            greeting = ""
            body = s

        # Detect signoff
        signoff_patterns = [
            (r"[,.]?\s*\b(best regards|kind regards|warm regards)[,.]?\s*$", "\n\nBest regards,"),
            (r"[,.]?\s*\b(thanks|thank you)[,.]?\s*$", "\n\nThanks,"),
            (r"[,.]?\s*\b(cheers)[,.]?\s*$", "\n\nCheers,"),
            (r"[,.]?\s*\b(sincerely)[,.]?\s*$", "\n\nSincerely,"),
        ]
        for pat, repl in signoff_patterns:
            if re.search(pat, body, flags=re.IGNORECASE):
                body = re.sub(pat, repl, body, flags=re.IGNORECASE)
                break

        return greeting + body

    def _format_bullets(self, text: str) -> str:
        # Split on list trigger words
        split_pat = r"(?:\b(?:firstly|first|secondly|second|thirdly|third|finally|next|also|additionally|number \d+|point \d+)\b[:,-]?)"
        parts = re.split(split_pat, text, flags=re.IGNORECASE)

        items = [p.strip() for p in parts if p.strip()]
        if len(items) <= 1:
            sentences = [s.strip() for s in re.split(r"[.?!]\s+", text) if s.strip()]
            if len(sentences) > 1:
                items = sentences
            else:
                items = [text.strip()]

        lines = []
        for item in items:
            if item:
                capitalized = item[0].upper() + item[1:]
                if not capitalized.endswith((".", "!", "?")):
                    capitalized += "."
                lines.append(f"• {capitalized}")

        return "\n".join(lines)
