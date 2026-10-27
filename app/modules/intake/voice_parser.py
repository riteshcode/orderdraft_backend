"""
Voice note -> text, Deepgram se.

Docs: https://developers.deepgram.com/reference/speech-to-text-api/listen
- language=en-AU  : Australian accent
- smart_format    : numbers ko digits mein likhta hai ("two boxes" -> "2 boxes")
- keyterm         : (nova-3) product names ka hint, taaki "roma" ko "roamer" na likhe
"""
import httpx

from app.core.config import settings

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"


class TranscriptionError(Exception):
    pass


def transcribe_audio(content: bytes, content_type: str = "audio/mpeg", keyterms: list[str] | None = None,
                     on_usage=None) -> str:
    """on_usage(seconds) - billing/cost tracking ke liye audio duration batata hai."""
    if not settings.DEEPGRAM_API_KEY:
        raise TranscriptionError("Voice transcription isn't set up yet (DEEPGRAM_API_KEY)")

    # httpx list of tuples se repeated query params bana deta hai (keyterm=a&keyterm=b)
    params = [
        ("model", settings.DEEPGRAM_MODEL),
        ("language", settings.DEEPGRAM_LANGUAGE),
        ("smart_format", "true"),
        ("punctuate", "true"),
    ]
    for term in (keyterms or [])[:50]:   # zyada terms se accuracy girti hai
        params.append(("keyterm", term))

    headers = {
        "Authorization": f"Token {settings.DEEPGRAM_API_KEY}",
        "Content-Type": content_type or "audio/mpeg",
    }

    try:
        resp = httpx.post(DEEPGRAM_URL, params=params, headers=headers, content=content, timeout=120)
    except httpx.HTTPError as e:
        raise TranscriptionError(f"Deepgram request failed: {e}")

    if resp.status_code != 200:
        raise TranscriptionError(f"Deepgram error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    try:
        transcript = data["results"]["channels"][0]["alternatives"][0]["transcript"]
    except (KeyError, IndexError):
        raise TranscriptionError("No transcript was found in the Deepgram response")

    if on_usage:
        on_usage(float((data.get("metadata") or {}).get("duration") or 0))
    return transcript.strip()
