import uuid as uuid_mod

from celery import Celery

from .config import settings
from .database import SessionLocal
from .scheduler.solver import solve_period
from .scheduler.validator import validate_period


celery_app = Celery(
    "cse_scheduler",
    broker=settings.redis_url,
    backend=settings.redis_url,
)


@celery_app.task
def solve_period_task(period_id: str):
    db = SessionLocal()

    try:
        result = solve_period(
            db,
            uuid_mod.UUID(str(period_id)),
        )
        return result
    finally:
        db.close()


@celery_app.task
def validate_period_task(period_id: str):
    db = SessionLocal()

    try:
        return validate_period(
            db,
            uuid_mod.UUID(str(period_id)),
        )
    finally:
        db.close()
