"""
order_state_machine.py — Non-Linear Order State Machine & Surgical Diffs
========================================================================
Enables MIRA CRM to handle mid-sentence modifications, non-linear rollbacks,
and corrections without restarting the ordering workflow.

Examples:
- "Actually, can you change the medium pepperoni to large and add five diet cokes"
- "ନା ଭାଇ, ବିରିୟାନୀ କାଟିଦିଅ, ତା' ବଦଳରେ ଦୁଇଟା ମସାଲା ଦୋସା କରିଦିଅ"
- "ପେପରୋନି ପିଜା ତିନିଟା ବଦଳରେ ଗୋଟେ କରିଦିଅ"
"""

import copy
import difflib
import re
from typing import Dict, Any, List, Optional, Tuple


MENU_CATALOG: Dict[str, Dict[str, Any]] = {
    "pepperoni_pizza": {
        "name_odia": "ପେପରୋନି ପିଜା",
        "name_en": "Pepperoni Pizza",
        "aliases": ["pepperoni", "pepperoni pizza", "ପେପରୋନି", "ପେପରୋନି ପିଜା", "pizza", "ପିଜା", "ପିଜ୍ଜା"],
        "base_price": 280.0,
        "sizes": {"small": 220.0, "medium": 280.0, "large": 380.0},
        "default_size": "medium"
    },
    "veggie_supreme": {
        "name_odia": "ଭେଜ୍ ସୁପ୍ରିମ୍ ପିଜା",
        "name_en": "Veggie Supreme Pizza",
        "aliases": ["veggie supreme", "veggie pizza", "ଭେଜ୍ ପିଜା", "ଭେଜି ସୁପ୍ରିମ", "ଭେଜ୍ ସୁପ୍ରିମ୍", "ଭେଜ୍ ସୁପ୍ରିମ୍ ପିଜା", "ଭେଜି ପିଜା"],
        "base_price": 240.0,
        "sizes": {"small": 190.0, "medium": 240.0, "large": 340.0},
        "default_size": "medium"
    },
    "diet_coke": {
        "name_odia": "ଡାଏଟ୍ କୋକ୍",
        "name_en": "Diet Coke",
        "aliases": ["diet coke", "coke", "diet cokes", "diet", "କୋକ୍", "ଡାଏଟ୍ କୋକ୍", "ଡାଏଟ", "ଡାଇଟ", "ଡାୟେଟ", "କୋକା", "କୋଲା", "ଡାଏଟ୍ କୋଟ", "ନାଇଟ୍ କୋପ", "ଡାଏଟ୍ କୋଲା"],
        "base_price": 40.0,
        "sizes": {"regular": 40.0},
        "default_size": "regular"
    },
    "chicken_biryani": {
        "name_odia": "ଚିକେନ ବିରିୟାନୀ",
        "name_en": "Chicken Biryani",
        "aliases": ["biryani", "chicken biryani", "ବିରିୟାନୀ", "ଚିକେନ ବିରିୟାନୀ"],
        "base_price": 180.0,
        "sizes": {"half": 110.0, "full": 180.0, "family": 340.0},
        "default_size": "full"
    },
    "veg_burger": {
        "name_odia": "ଭେଜ୍ ବର୍ଗର",
        "name_en": "Veg Burger",
        "aliases": ["veg burger", "veggie burger", "ଭେଜ୍ ବର୍ଗର", "ଭେଜ ବର୍ଗର", "ଭେଜ୍ ବରଗର"],
        "base_price": 90.0,
        "sizes": {"regular": 90.0},
        "default_size": "regular"
    },
    "chicken_burger": {
        "name_odia": "ଚିକେନ ବର୍ଗର",
        "name_en": "Chicken Burger",
        "aliases": ["chicken burger", "burger", "ଚିକେନ ବର୍ଗର", "ଚିକେନ୍ ବର୍ଗର୍", "ଚିକେନ ବରଗର", "ବର୍ଗର", "ବରଗର", "ବର୍ଗାର", "ବର୍ଗର୍"],
        "base_price": 120.0,
        "sizes": {"regular": 120.0},
        "default_size": "regular"
    },
    "french_fries": {
        "name_odia": "ଫ୍ରେଞ୍ଚ ଫ୍ରାଇ",
        "name_en": "French Fries",
        "aliases": ["french fries", "fries", "ଫ୍ରେଞ୍ଚ ଫ୍ରାଇ", "ଫ୍ରାଇସ୍", "ଫ୍ରାଇ"],
        "base_price": 80.0,
        "sizes": {"regular": 80.0},
        "default_size": "regular"
    },
    "veg_biryani": {
        "name_odia": "ଭେଜ୍ ବିରିୟାନୀ",
        "name_en": "Veg Biryani",
        "aliases": ["veg biryani", "veggie biryani", "ଭେଜ୍ ବିରିୟାନୀ", "ଭେଜ ବିରିୟାନୀ", "ଭେଜିଟେବଲ ବିରିୟାନୀ"],
        "base_price": 150.0,
        "sizes": {"half": 95.0, "full": 150.0, "family": 290.0},
        "default_size": "full"
    },
    "masala_dosa": {
        "name_odia": "ମସାଲା ଦୋସା",
        "name_en": "Masala Dosa",
        "aliases": ["dosa", "masala dosa", "ଦୋସା", "ମସାଲା ଦୋସା"],
        "base_price": 70.0,
        "sizes": {"regular": 70.0, "special": 95.0},
        "default_size": "regular"
    }
}

QUANTITY_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "ଗୋଟେ": 1, "ଗୋଟିଏ": 1, "ଏକ": 1, "1": 1,
    "ଦିଟା": 2, "ଦିଟି": 2, "ଦିଇଟା": 2, "ଦୁଇ": 2, "ଦୁଇଟା": 2, "ଦୁଇଟି": 2, "ଦୁଟା": 2, "ଦୁଇଟ": 2, "2": 2,
    "ତିନୋଟା": 3, "ତିନି": 3, "ତିନିଟା": 3, "ତିନିଟି": 3, "3": 3,
    "ଚାରି": 4, "ଚାରିଟା": 4, "ଚାରିଟି": 4, "4": 4,
    "ପାଞ୍ଚ": 5, "ପାଞ୍ଚଟା": 5, "ପାଞ୍ଚଟି": 5, "5": 5,
    "ଛଅ": 6, "ଛଅଟା": 6, "ଛଅଟି": 6, "6": 6
}

SIZE_ODIA = {"small": "ଛୋଟ", "medium": "ମିଡିୟମ୍", "large": "ବଡ଼", "half": "ହାଫ୍", "full": "ଫୁଲ୍"}

SIZE_WORDS = {
    "small": "small", "chhota": "small", "ଛୋଟ": "small",
    "medium": "medium", "ମିଡିୟମ୍": "medium", "ମଧ୍ୟମ": "medium",
    "large": "large", "bada": "large", "ବଡ଼": "large", "ଲାର୍ଜ୍": "large",
    "half": "half", "ହାଫ୍": "half",
    "full": "full", "ଫୁଲ୍": "full"
}


# The Odia recognizer spells English food words differently every time (ପେପରନି / ପରୋନି, ମସଲ ଦୋଷ),
# so aliases are also compared by a consonant "skeleton" that ignores vowels, halants and look-alike letters.
_SKEL_MAP = {}
for _group in ("କଖଗଘ", "ଚଛଜଝଯ", "ଟଠଡଢତଥଦଧ", "ପଫବଭ", "ସଶଷ", "ନଣ", "ଲଳ"):  # ଡ଼ଢ଼ lose their nukta in skeleton(): they fall into the ଟ group
    for _c in _group:
        _SKEL_MAP[_c] = _group[0]
FUZZY_MIN_RATIO = 0.8
FUZZY_LONG_RATIO = 0.75  # aliases of 5+ consonants: one wrong final consonant ("କୋକ"/"କୋଟ") is still the same item


def skeleton(text: str) -> str:
    out = []
    for ch in text.lower():
        if "କ" <= ch <= "ହ" or ch in "ଡ଼ଢ଼ୟୱ":
            out.append(_SKEL_MAP.get(ch, ch))
        elif ch.isascii() and ch.isalpha():
            out.append(ch)  # English aliases only ever match exactly, kept for the length check
    return "".join(out)


_OPENERS = ["", "", "ଠିକ୍ ଅଛି, ", "ହଁ, ", "ଆଜ୍ଞା, "]  # often none: a filler on every sentence is what sounds scripted
_last_opener = [None]


_last_pick: dict = {}


def pick(key: str, *variants: str) -> str:
    """One of several phrasings, never the same one twice in a row for this key."""
    import random
    options = [v for v in variants if v != _last_pick.get(key)] or list(variants)
    _last_pick[key] = random.choice(options)
    return _last_pick[key]


def opener() -> str:
    """A different lead-in from last time (or none), so replies don't all start the same way."""
    import random
    pick = random.choice([o for o in _OPENERS if o != _last_opener[0]])
    _last_opener[0] = pick
    return pick


class OrderCartItem:
    def __init__(self, sku: str, name_odia: str, name_en: str, quantity: int, size: str, unit_price: float):
        self.sku = sku
        self.name_odia = name_odia
        self.name_en = name_en
        self.quantity = quantity
        self.size = size
        self.unit_price = unit_price

    @property
    def total_price(self) -> float:
        return round(self.quantity * self.unit_price, 2)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sku": self.sku,
            "name_odia": self.name_odia,
            "name_en": self.name_en,
            "quantity": self.quantity,
            "size": self.size,
            "unit_price": self.unit_price,
            "total_price": self.total_price
        }


# "scratch that", "never mind": take back the last change. Whatever follows is a new instruction ("scratch that, make it large").
UNDO_PHRASES = ("scratch that", "forget that", "never mind", "nevermind", "ignore that", "cancel that", "take that back",
                "ଛାଡ଼ିଦିଅ", "ଛାଡି ଦିଅ", "ଛାଡ଼ି ଦିଅ", "ଭୁଲ ହେଲା", "ଭୁଲ ହୋଇଗଲା", "ସେଟା ବାଦ୍", "ଏଇଟା ବାଦ୍")
ASK_MARKERS = ("ଅଛି", "ମିଳେ", "do you have", "is there", "available")   # "is there veg biryani?": a question, not an order
ORDER_VERBS = ("କର", "ଦିଅ", "ଯୋଡ଼", "ଚାହିଁ", "ଚାହେଁ", "ଦରକାର", "add", "want", "order", "give", "need", "ଆଡ")
ALL_WORDS = ("ସବୁ", "ସମସ୍ତ", "ପ୍ରତ୍ୟେକ", "all", "each", "every")


class OrderStateMachine:
    def __init__(self, catalog: Optional[Dict[str, Dict[str, Any]]] = None):
        self.catalog = catalog or MENU_CATALOG
        self._misses = 0  # consecutive turns where nothing was understood
        self.items: Dict[str, OrderCartItem] = {}
        # Event sourcing ledger for rollbacks
        self.history_snapshots: List[Dict[str, Any]] = []

    def _snapshot(self):
        """Saves current state into history snapshot stack."""
        snap = {
            sku: OrderCartItem(
                it.sku, it.name_odia, it.name_en, it.quantity, it.size, it.unit_price
            )
            for sku, it in self.items.items()
        }
        self.history_snapshots.append(snap)

    def rollback(self) -> Tuple[bool, str]:
        """Rolls back to the previous turn's cart state."""
        if not self.history_snapshots:
            return False, "କୌଣସି ପୂର୍ବ ଅର୍ଡର ସ୍ଥିତି ମିଳିଲା ନାହିଁ ।"
        prev = self.history_snapshots.pop()
        self.items = prev
        return True, "ପୂର୍ବ ଅର୍ଡର ସ୍ଥିତିକୁ ଫେରାଇ ଅଣାଗଲା ।"

    def match_sku(self, text: str) -> Optional[str]:
        """Matches spoken phrase against catalog aliases."""
        t_low = text.lower()
        best, best_len = None, 0
        for sku, meta in self.catalog.items():  # longest exact alias wins ("veg burger" over "burger")
            for alias in meta["aliases"]:
                if alias in t_low and len(alias) > best_len:
                    best, best_len = sku, len(alias)
        if best:
            return best
        words = t_low.split()
        windows = [skeleton("".join(words[i:i + n])) for n in (1, 2, 3) for i in range(len(words) - n + 1)]
        top, top_score = None, FUZZY_LONG_RATIO
        for sku, meta in self.catalog.items():
            for alias in meta["aliases"]:
                a = skeleton(alias)
                if len(a) < 3 or alias.isascii():
                    continue
                for w in windows:
                    score = difflib.SequenceMatcher(None, a, w).ratio() if len(w) >= 3 else 0
                    if score > top_score:
                        top, top_score = sku, score
        return top

    def process_turn(self, utterance: str) -> Dict[str, Any]:
        """
        Parses customer utterance, generates surgical diffs, updates state,
        and formulates natural Odia & English confirmation speech.
        Supports mid-sentence modifications, non-linear rollbacks, and multi-clause additions.
        """
        u_low = utterance.lower().strip()
        diffs = []

        # 0. "scratch that, make it large": undo the last change, then act on what comes after
        for phrase in UNDO_PHRASES:
            if phrase in u_low:
                self._misses = 0
                ok, msg = self.rollback()
                rest = utterance[u_low.index(phrase) + len(phrase):].strip(" ,.;!?ଏବଂ")
                if ok and len(rest) >= 2 and self.match_sku(rest):
                    return self.process_turn(rest)
                return {
                    "action": "rollback", "diffs": [{"action": "rollback"}], "cart": self.get_cart_summary(),
                    "response_odia": msg + (" ଆଉ କ'ଣ ଚାହାଁନ୍ତି ?" if ok else ""),
                    "response_en": "Okay, I've taken that back. What would you like instead?" if ok else "There is nothing to take back.",
                }

        # 1. Check for explicit rollback / undo triggers
        if any(w in u_low for w in ["undo", "rollback", "ପୂର୍ବ ଅର୍ଡର", "ଆଗ ଭଳି"]):
            success, msg = self.rollback()
            return {
                "action": "rollback",
                "diffs": [{"action": "rollback"}],
                "cart": self.get_cart_summary(),
                "response_odia": msg,
                "response_en": "I've rolled back your order to the previous state."
            }

        # 1b. "ଭେଜ୍ ବିରିୟାନୀ ଅଛି?": asking if it is available, not ordering it
        ask_sku = self.match_sku(utterance)
        if (ask_sku and any(m in u_low for m in ASK_MARKERS) and not any(v in u_low for v in ORDER_VERBS)
                and self._extract_quantity(utterance) is None):
            meta = self.catalog[ask_sku]
            price = int(meta["sizes"].get(meta["default_size"], meta["base_price"]))
            self._misses = 0
            return {
                "action": "ask", "diffs": [], "cart": self.get_cart_summary(),
                "response_odia": f"ହଁ, {meta['name_odia']} ଅଛି, {price} ଟଙ୍କା । କେତେଟା ଦେବି ?",
                "response_en": f"Yes, we have {meta['name_en']} at ₹{price}. How many would you like?",
            }

        # 1c. "ସବୁ ଦୁଇଟା ଦୁଇଟା କରିଦିଅ": one quantity for every item already in the cart
        all_qty = self._extract_quantity(utterance)
        named = any(al in u_low for m in self.catalog.values() for al in m["aliases"])  # exact only: fuzzy would read "ଦୁଇଟା ଦୁଇଟା" as an item
        if self.items and all_qty and not named and any(w in u_low.split() for w in ALL_WORDS):
            self._snapshot()
            for it in self.items.values():
                it.quantity = all_qty
            return {
                "action": "set_all", "diffs": [{"action": "set_all", "qty": all_qty}], "cart": self.get_cart_summary(),
                "response_odia": f"ଆଚ୍ଛା, ସବୁ ଆଇଟମ୍ {all_qty} ଟି କରିଦେଲି । ମୋଟ {int(self.get_cart_summary()['grand_total'])} ଟଙ୍କା ହେଲା ।",
                "response_en": f"Okay, every item is now {all_qty}. Total ₹{int(self.get_cart_summary()['grand_total'])}.",
            }

        self._snapshot()

        # 2. Decompose multi-intent utterance into logical grammatical clauses
        clauses = [
            c.strip()
            for c in re.split(
                r'[,;]|\s+(?:and\s+also|and\s+add|and|also|plus|ଆଉ|ଏବଂ|ତଥା)\s+',
                utterance,
                flags=re.IGNORECASE
            )
            if c.strip()
        ]

        for clause in clauses:
            c_low = clause.lower()

            # A. Check modification / size change (e.g. "change pepperoni to large", "ପେପରୋନି ବଡ଼ କରିଦିଅ")
            mod_m = re.search(
                r'(?:change|make|modify|କର|କରିଦିଅ|ବଦଳାଅ)\s+(?:the\s+)?([a-zA-Z\u0B00-\u0B7F\s]+?)\s+(?:to|in|ରେ)\s+(\w+)',
                clause,
                re.IGNORECASE
            )
            if not mod_m:  # Odia order of words: "<item> <size> କରିଦିଅ" ("make the pepperoni large")
                mod_m = re.search(r'([a-zA-Z଀-୿\s]+?)\s+(\S+)\s+(?:କରିଦିଅ|କରିଦିଏ|କରିଦେବ|କରନ୍ତୁ|କର)\b', clause)
                if mod_m and mod_m.group(2).strip().lower() not in SIZE_WORDS:
                    mod_m = None
            if mod_m:
                it_phr = mod_m.group(1).strip().lower()
                new_sz_w = mod_m.group(2).strip().lower()
                sku = self.match_sku(it_phr)
                if sku and sku in self.items and new_sz_w in SIZE_WORDS:
                    new_sz = SIZE_WORDS[new_sz_w]
                    old_sz = self.items[sku].size
                    self.items[sku].size = new_sz
                    new_p = self.catalog[sku]["sizes"].get(new_sz, self.items[sku].unit_price)
                    self.items[sku].unit_price = new_p
                    diffs.append({
                        "action": "modify",
                        "target": sku,
                        "field": "size",
                        "old_value": old_sz,
                        "new_value": new_sz,
                        "new_price": new_p
                    })
                    continue

            # B. Check removal (e.g. "remove biryani", "ବିରିୟାନୀ କାଟିଦିଅ")
            rem_m = (
                re.search(r'(?:remove|cancel|drop|delete|କାଟିଦିଅ|ହଟାଅ|ବାଦ୍)\s+(?:the\s+)?([a-zA-Z\u0B00-\u0B7F\s]+)', clause, re.IGNORECASE)
                or re.search(r'([a-zA-Z\u0B00-\u0B7F\s]+?)\s+(?:କାଟିଦିଅ|ହଟାଇଦିଅ|ଦରକାର\s+ନାହିଁ)', clause, re.IGNORECASE)
            )
            if rem_m:
                it_phr = rem_m.group(1).strip().lower()
                sku = self.match_sku(it_phr)
                if sku and sku in self.items:
                    rem_it = self.items.pop(sku)
                    diffs.append({"action": "remove", "target": sku, "qty": rem_it.quantity})
                    continue

            # C. Check replacement (e.g. "ବିରିୟାନୀ ବଦଳରେ ଦୁଇଟା ଦୋସା")
            repl_m = re.search(
                r'([a-zA-Z\u0B00-\u0B7F\s]+?)\s+(?:instead\s+of|badalre|ବଦଳରେ)\s+([a-zA-Z\u0B00-\u0B7F\s]+)',
                clause,
                re.IGNORECASE
            )
            if repl_m:
                new_part = repl_m.group(1).strip()
                old_part = repl_m.group(2).strip()
                old_sku = self.match_sku(old_part)
                new_sku = self.match_sku(new_part)
                if old_sku and old_sku in self.items:
                    rem_it = self.items.pop(old_sku)
                    diffs.append({"action": "remove", "target": old_sku, "qty": rem_it.quantity})
                if new_sku:
                    qty = self._extract_quantity(new_part) or 1
                    meta = self.catalog[new_sku]
                    size = meta["default_size"]
                    price = meta["sizes"].get(size, meta["base_price"])
                    self.items[new_sku] = OrderCartItem(new_sku, meta["name_odia"], meta["name_en"], qty, size, price)
                    diffs.append({"action": "add", "target": new_sku, "qty": qty, "size": size})
                continue

            # D. Item addition / placement (e.g. "five diet cokes", "ଗୋଟେ ବିରିୟାନୀ")
            sku = self.match_sku(clause)
            if sku:
                qty = self._extract_quantity(clause) or 1
                meta = self.catalog[sku]
                size = meta["default_size"]
                for sz_w, sz_val in SIZE_WORDS.items():
                    if sz_w in c_low:
                        size = sz_val
                        break
                price = meta["sizes"].get(size, meta["base_price"])
                if sku in self.items:
                    # Increment if not already modified in this turn
                    if not any(d.get("target") == sku for d in diffs):
                        self.items[sku].quantity += qty
                        diffs.append({"action": "increment", "target": sku, "qty_delta": qty})
                else:
                    self.items[sku] = OrderCartItem(sku, meta["name_odia"], meta["name_en"], qty, size, price)
                    diffs.append({"action": "add", "target": sku, "qty": qty, "size": size})

        # 3. Formulate natural spoken Odia & English confirmations
        resp_odia = self._formulate_diff_response_odia(diffs)
        resp_en = self._formulate_diff_response_en(diffs)
        self._misses = 0 if diffs else self._misses + 1

        return {
            "diffs": diffs,
            "cart": self.get_cart_summary(),
            "response_odia": resp_odia,
            "response_en": resp_en
        }

    # Alias for process_turn
    process_utterance = process_turn

    def _extract_quantity(self, text: str) -> Optional[int]:
        tokens = text.lower().split()
        for tok in tokens:
            if tok in QUANTITY_WORDS:
                return QUANTITY_WORDS[tok]
            if tok.isdigit():
                return int(tok)
        return None

    def _formulate_diff_response_odia(self, diffs: List[Dict[str, Any]]) -> str:
        if not diffs:
            if self._misses == 0:
                return "କ୍ଷମା କରନ୍ତୁ, ମୁଁ ବୁଝିପାରିଲି ନାହିଁ । ଦୟାକରି ପୁଣି କୁହନ୍ତୁ ।"
            menu = ", ".join(m["name_odia"] for m in self.catalog.values())
            return f"ମୁଁ ବୁଝିପାରୁନାହିଁ । ଦୟାକରି ଗୋଟିଏ ଜଣ ଗୋଟିଏ ଗୋଟିଏ କରି କୁହନ୍ତୁ । ଆମ ପାଖରେ ଅଛି: {menu} ।"  # two misses in a row is also what overlapping voices sound like

        phrases = []
        for d in diffs:
            t = d.get("target", "")
            meta = self.catalog.get(t, {})
            name_odia = meta.get("name_odia", t)

            if d["action"] == "modify" and d.get("field") == "size":
                new_sz = SIZE_ODIA.get(d['new_value'], d['new_value'])  # Latin text would be read letter by letter
                phrases.append(f"{name_odia}କୁ {new_sz} ସାଇଜ୍ କରିଦେଲି")
            elif d["action"] == "add":
                phrases.append(f"{d['qty']} ଟି {name_odia} {pick('add', 'ଯୋଡ଼ିଦେଲି', 'ଲେଖିନେଲି', 'ରଖିଦେଲି')}")  # spaced: a digit glued to Odia letters is not read as a number
            elif d["action"] == "increment":
                phrases.append(f"ଆଉ {d['qty_delta']} ଟି {name_odia} ବଢ଼ାଇଦେଲି")
            elif d["action"] == "remove":
                phrases.append(f"{name_odia} ହଟାଇଦେଲି")

        joined = " ଏବଂ ".join(phrases)
        total = self.get_cart_summary()["grand_total"]
        t = int(total)
        total_line = pick("total", f"ଏବେ ମୋଟ {t} ଟଙ୍କା ହେଲା", f"ମୋଟ {t} ଟଙ୍କା ହେଉଛି", f"ସବୁ ମିଶି {t} ଟଙ୍କା ହେଲା")
        more = pick("more", " ଆଉ କିଛି ଚାହିଁବେ ?", "", " ଆଉ କିଛି ?")  # sometimes none: a question every time sounds scripted
        return f"{opener()}{joined} । {total_line} ।{more}"  # two short sentences: the first is spoken while the second renders

    def _formulate_diff_response_en(self, diffs: List[Dict[str, Any]]) -> str:
        if not diffs:
            if self._misses == 0:
                return "Sorry, I didn't catch that. Please say it again."
            menu = ", ".join(m["name_en"] for m in self.catalog.values())
            return f"I'm having trouble understanding. Please speak one person at a time. We have: {menu}."

        phrases = []
        for d in diffs:
            t = d.get("target", "")
            meta = self.catalog.get(t, {})
            name_en = meta.get("name_en", t)

            if d["action"] == "modify" and d.get("field") == "size":
                new_sz = d['new_value'].capitalize()
                phrases.append(f"changed {name_en} to {new_sz}")
            elif d["action"] == "add":
                phrases.append(f"added {d['qty']} {name_en}")
            elif d["action"] == "increment":
                phrases.append(f"added {d['qty_delta']} more {name_en}")
            elif d["action"] == "remove":
                phrases.append(f"removed {name_en}")

        joined = ", and ".join(phrases)
        total = self.get_cart_summary()["grand_total"]
        return f"Sure! I've {joined}. Your total is ₹{int(total)}."

    _formulate_diff_response = _formulate_diff_response_odia

    def get_cart_summary(self) -> Dict[str, Any]:
        items_list = [it.to_dict() for it in self.items.values()]
        grand_total = round(sum(it.total_price for it in self.items.values()), 2)
        total_items = sum(it.quantity for it in self.items.values())
        return {
            "items": items_list,
            "item_count": len(items_list),
            "total_items": total_items,
            "total_quantity": total_items,
            "grand_total": grand_total,
            "total_price": grand_total,
            "history_version": len(self.history_snapshots)
        }