"""Application factory, lifecycle, and the session timeline."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from saronic_api.config import Settings
from saronic_api.database import build_engine, check_database
from saronic_api.routes import router as sessions_router

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = settings or Settings()
        logging.basicConfig(level=config.log_level.upper())
        engine = build_engine(config)
        app.state.settings = config
        app.state.engine = engine
        try:
            await run_in_threadpool(check_database, engine)
            logger.info("Database connection verified; API foundation ready")
            yield
        finally:
            await run_in_threadpool(engine.dispose)

    app = FastAPI(
        title="Saronic API",
        version="0.1.0",
        description=(
            "Session timeline for simulation runs. Provisioning and simulation control "
            "are not implemented yet."
        ),
        lifespan=lifespan,
    )
    app.include_router(sessions_router)
    return app


app = create_app()
