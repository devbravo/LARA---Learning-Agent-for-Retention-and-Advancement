import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from src.api.routes import health, scheduler_status, webhook
from src.api.routes.mcp import mcp_app
from src.infrastructure.scheduler import build_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = build_scheduler()
    scheduler.start()
    app.state.scheduler = scheduler
    yield
    scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    _dev = os.getenv("ENV", "production").lower() != "production"
    app = FastAPI(
        title="LARA",
        docs_url="/docs" if _dev else None,
        redoc_url="/redoc" if _dev else None,
        lifespan=lifespan,
        redirect_slashes=False,
    )
    app.include_router(health.router)
    app.include_router(webhook.router)
    app.include_router(scheduler_status.router)
    app.mount("/mcp", mcp_app)
    return app


app = create_app()