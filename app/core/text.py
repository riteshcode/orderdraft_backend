"""
Text / Unicode helpers - har input (form, CSV, email, accounting sync) se aaya text yahi se saaf hota hai.

clean_text("Café​  Ｔａｂｌｅ")  ->  "Café  Ｔａｂｌｅ"
  - NFC normalize (é ek hi character - search / match / unique sahi chale)
  - zero-width / BOM / control characters hatao (copy-paste se aate hain, dikhte nahi par match tod dete hain)
  - non-breaking space -> normal space
  - emoji, Hindi, Chinese, accents sab rehte hain (MySQL utf8mb4 mein store hote hain)

Database save se pehle har String / Text column pe apne aap lagta hai (core/database.py before_flush).
"""
import re
import unicodedata
from urllib.parse import quote

INVISIBLE = re.compile("[​‌‍⁠﻿­]")            # zero-width, BOM, soft hyphen
CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")                 # \n \r \t rehne do
SPACES = re.compile("[   ]")                                   # non-breaking spaces


def clean_text(value):
    """String ho to saaf karo, baaki (None, number) waise hi."""
    if not isinstance(value, str) or not value:
        return value
    if value.isascii() and not CONTROL.search(value):
        return value                                   # fast path - zyada tar text ASCII hota hai
    value = unicodedata.normalize("NFC", value)
    value = INVISIBLE.sub("", value)
    value = SPACES.sub(" ", value)
    return CONTROL.sub("", value)


def decode_bytes(content: bytes) -> str:
    """CSV / text file: UTF-8 (BOM ke saath bhi), warna Windows Excel ka cp1252, warna latin-1. Kabhi crash nahi."""
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1")


def content_disposition(filename: str, inline: bool = False) -> str:
    """Download header jo kisi bhi naam (é, 中文, emoji) pe nahi tootega (RFC 6266 / 5987)."""
    filename = clean_text(filename).replace('"', "").replace("\n", " ").replace("\r", " ")
    ascii_name = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode() or "download"
    kind = "inline" if inline else "attachment"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
