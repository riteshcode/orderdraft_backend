"""
Background jobs ek jagah se. Code mein bas:
    enqueue(process_message, message_id)

JOBS_MODE:
  thread - app ke andar alag thread (local / Windows - Redis nahi chahiye)
  rq     - Redis queue; worker alag chalao:  python -m app.worker
  sync   - turant (tests)

Job functions ko hamesha simple args (ids) do, objects nahi -
kyunki rq unko serialize karke doosre process mein bhejta hai.
"""
import logging
import threading
import traceback

from app.core.config import settings

log = logging.getLogger("jobs")


def _safe_run(func, *args):
    try:
        func(*args)
    except Exception:
        log.error("Job %s%s failed:\n%s", func.__name__, args, traceback.format_exc())


def enqueue(func, *args):
    mode = settings.JOBS_MODE

    if mode == "sync":
        _safe_run(func, *args)
        return

    if mode == "rq":
        from redis import Redis
        from rq import Queue

        queue = Queue("default", connection=Redis.from_url(settings.REDIS_URL))
        queue.enqueue(func, *args, job_timeout=600)
        return

    # default: thread
    threading.Thread(target=_safe_run, args=(func, *args), daemon=True).start()
