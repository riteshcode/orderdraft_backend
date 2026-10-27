"""
RQ worker (production, JOBS_MODE=rq). Linux/Mac server pe chalao:
    python -m app.worker

Windows pe RQ nahi chalta (fork use karta hai) - wahan JOBS_MODE=thread rakho.
"""
import logging

from redis import Redis
from rq import Queue, Worker

from app.core.config import settings

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    redis = Redis.from_url(settings.REDIS_URL)
    Worker([Queue("default", connection=redis)], connection=redis).work()
