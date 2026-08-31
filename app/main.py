from fastapi import FastAPI
from sqlalchemy import text

from app.db import SessionLocal

app = FastAPI(title="Tratte", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    with SessionLocal() as session:
        session.execute(text("SELECT 1"))
    return {"status": "ok"}
