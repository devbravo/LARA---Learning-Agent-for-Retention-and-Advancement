from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.routes import health, scheduler_status, webhook
from src.infrastructure.scheduler import build_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = build_scheduler()
    scheduler.start()
    app.state.scheduler = scheduler
    yield
    scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    app = FastAPI(title="LARA", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(webhook.router)
    app.include_router(scheduler_status.router)
    return app


app = create_app()
