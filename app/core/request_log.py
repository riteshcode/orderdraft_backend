"""
Request log + error reference - support ke liye "backend pe jaake dekhna".

Har API request ki ek line  logs/app.log  mein (JSON, roz naya file, LOG_KEEP_DAYS din tak):
    {"time": "...", "level": "INFO", "request_id": "a1b2c3d4e5f6", "method": "PUT", "path": "/products/12",
     "status": 200, "ms": 34, "tenant_id": 3, "user_id": 7, "ip": "1.2.3.4"}

Error (500) aaye to:
  - poora traceback isi request_id ke saath log mein
  - user ko message: "Something went wrong. Reference: a1b2c3d4e5f6"  -> customer ye number support ko bhejta hai
  - support:  grep a1b2c3d4e5f6 logs/app.log*

Har response mein header  X-Request-ID  bhi aata hai (browser DevTools mein dikh jaata hai).
Kya badla (purana -> naya) wo alag hai: Activity log (core/audit.py), app ke andar dikhta hai.
"""
import json
import logging
import os
import time
import traceback
import uuid
from logging.handlers import TimedRotatingFileHandler

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.config import settings

logger = logging.getLogger("orderdraft.requests")

SKIP_PATHS = ("/health",)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {"time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname}
        data.update(getattr(record, "extra_data", {}) or {})
        if record.getMessage() and record.getMessage() != "request":
            data["message"] = record.getMessage()
        return json.dumps(data, ensure_ascii=False, default=str)


def setup_file_logging():
    """logs/app.log (JSON lines). Ek hi baar handler lagta hai."""
    if any(getattr(h, "_od", False) for h in logger.handlers):
        return
    os.makedirs(settings.LOG_DIR, exist_ok=True)
    handler = TimedRotatingFileHandler(os.path.join(settings.LOG_DIR, "app.log"), when="midnight",
                                       backupCount=settings.LOG_KEEP_DAYS, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    handler._od = True
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False   # console pe "request" spam nahi; poori line file mein


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def install(app: FastAPI):
    setup_file_logging()

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        # Proxy (nginx) ne request id di ho to wahi, warna nayi
        request_id = (request.headers.get("x-request-id") or uuid.uuid4().hex[:12])[:40]
        request.state.request_id = request_id
        started = time.perf_counter()
        error = None
        try:
            response = await call_next(request)
        except Exception as e:   # koi bhi unhandled error -> 500 + reference
            error = e
            response = JSONResponse(status_code=500, content={
                "detail": f"Something went wrong. Please contact support with reference {request_id}.",
                "error_id": request_id})
        response.headers["X-Request-ID"] = request_id
        if request.url.path in SKIP_PATHS or request.method == "OPTIONS":
            return response

        data = {
            "request_id": request_id, "method": request.method, "path": request.url.path,
            "query": str(request.url.query)[:300] or None, "status": response.status_code,
            "ms": round((time.perf_counter() - started) * 1000),
            "tenant_id": getattr(request.state, "tenant_id", None), "user_id": getattr(request.state, "user_id", None),
            "impersonator_id": getattr(request.state, "impersonator_id", None), "ip": _client_ip(request),
        }
        if error is not None:
            data["error"] = f"{type(error).__name__}: {error}"[:500]
            data["traceback"] = "".join(traceback.format_exception(error))[-4000:]
            logger.error("request", extra={"extra_data": data})
        elif response.status_code >= 500:
            logger.error("request", extra={"extra_data": data})
        elif response.status_code >= 400:
            logger.warning("request", extra={"extra_data": data})
        else:
            logger.info("request", extra={"extra_data": data})
        return response


def log_event(message: str, **data):
    """Request ke bahar ki important cheez (login fail, background job error) bhi isi file mein."""
    logger.warning(message, extra={"extra_data": data})
