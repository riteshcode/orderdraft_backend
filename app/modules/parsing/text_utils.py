"""
Chhote helper functions jo kai jagah use hote hain.
"""
import re

# Common abbreviations jo orders mein aate hain -> full form
# Isko apne clients ke hisaab se badhate jao
ABBREVIATIONS = {
    "tom": "tomato",
    "toms": "tomatoes",
    "pot": "potato",
    "pots": "potatoes",
    "ctn": "carton",
    "ctns": "cartons",
    "bx": "box",
    "pkt": "packet",
    "pk": "pack",
    "btl": "bottle",
    "btls": "bottles",
    "ea": "each",
    "doz": "dozen",
    "lge": "large",
    "lg": "large",
    "sml": "small",
    "med": "medium",
    "org": "organic",
}


def normalize(text: str | None) -> str:
    """
    Text ko matching ke liye saaf karo:
    "  2x Roma TOMS (10kg)!! " -> "roma tomatoes 10kg"
    """
    if not text:
        return ""
    t = text.lower()
    t = re.sub(r"[^\w\s\.]", " ", t)        # punctuation hatao (decimal point rehne do)
    t = re.sub(r"\b\d+\s*x\b", " ", t)      # "2x" jaisa quantity prefix hatao
    words = [ABBREVIATIONS.get(w, w) for w in t.split()]
    return " ".join(words).strip()


def clean_email(value: str | None) -> str | None:
    """'Joe <joe@cafe.com.au>' -> 'joe@cafe.com.au'"""
    if not value:
        return None
    match = re.search(r"[\w\.\-+]+@[\w\.\-]+\.\w+", value)
    return match.group(0).lower() if match else None


def clean_phone(value: str | None) -> str | None:
    """Sirf digits rakho, +61 ko 0 mein badlo: '+61 412 345 678' -> '0412345678'"""
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if digits.startswith("61") and len(digits) == 11:
        digits = "0" + digits[2:]
    return digits or None
