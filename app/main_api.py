"""Entrypoint FastAPI. Пока — health-check; сюда же ляжет REST для web (этап 2)."""
from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="burnfat API", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
