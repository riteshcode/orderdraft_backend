"""
Attachments aur voice files disk pe save karna.
Production mein isko S3 / R2 se replace kar sakte ho - baaki code same rahega.
"""
import os
import re
import uuid

from app.core.config import settings


def save_file(tenant_id: int, filename: str, content: bytes) -> str:
    """File save karo aur uska path return karo."""
    folder = os.path.join(settings.STORAGE_DIR, str(tenant_id))
    os.makedirs(folder, exist_ok=True)

    safe_name = re.sub(r"[^\w\.\-]", "_", filename or "file")[-100:]
    path = os.path.join(folder, f"{uuid.uuid4().hex[:10]}_{safe_name}")
    with open(path, "wb") as f:
        f.write(content)
    return path


def read_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def detect_kind(filename: str, content_type: str | None) -> str:
    """Attachment kis type ka hai: pdf | audio | image | csv | other"""
    name = (filename or "").lower()
    ctype = (content_type or "").lower()

    if name.endswith(".pdf") or "pdf" in ctype:
        return "pdf"
    if ctype.startswith("audio/") or name.endswith((".mp3", ".m4a", ".wav", ".ogg", ".opus", ".amr", ".aac", ".webm")):
        return "audio"
    if ctype.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
        return "image"
    if name.endswith((".csv", ".xlsx", ".xls")):
        return "sheet"
    return "other"
