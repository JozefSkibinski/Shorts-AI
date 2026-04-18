"""FastAPI entrypoint. Phase 1 only exposes /health and /trending stubs."""

from __future__ import annotations

from fastapi import FastAPI

from app.api.routes import trending


app = FastAPI(title="Shorts AI")
app.include_router(trending.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
