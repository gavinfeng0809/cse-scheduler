from fastapi import FastAPI
from sqlalchemy import text

from .database import engine
from .api import router
from .auth import router as auth_router
from .admin_api import router as admin_router

app = FastAPI(
    title="CSE 7x24 Scheduler",
    version="1.1.0",
)

app.include_router(auth_router, prefix="/api")
app.include_router(admin_router, prefix="/api")
app.include_router(router, prefix="/api")


@app.get("/health")
def health():
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {
        "status": "ok",
        "database": "ok",
    }
