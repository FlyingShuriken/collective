import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from pydantic.json_schema import models_json_schema

from collective.api.approval_routes import router as approval_router
from collective.api.auth import DatabaseDependency, JWTVerifier
from collective.api.dto import PUBLIC_MODELS, HealthResponse
from collective.api.errors import DomainError, install_error_handlers
from collective.api.event_routes import router as event_router
from collective.api.hub_routes import router as hub_router
from collective.api.job_routes import router as job_router
from collective.api.ledger_routes import router as ledger_router
from collective.api.need_routes import router as need_router
from collective.api.order_routes import router as order_router
from collective.api.pool_routes import router as pool_router
from collective.jobs.runner import Runner
from collective.repositories.db import Database
from collective.settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        db = Database(settings.database_url.get_secret_value()) if settings.database_url else None
        app.state.database = db
        worker = None
        try:
            if db is not None:
                await db.open()
                if settings.enable_worker:
                    worker = asyncio.create_task(Runner(db).run())
            yield
        finally:
            if worker is not None:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
            if db is not None:
                await db.close()

    app = FastAPI(title="Orbyte Collective", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.database = None
    app.state.verifier = JWTVerifier(settings) if settings.supabase_url else None
    install_error_handlers(app)
    app.include_router(hub_router)
    app.include_router(ledger_router)
    app.include_router(need_router)
    app.include_router(pool_router)
    app.include_router(approval_router)
    app.include_router(order_router)
    app.include_router(event_router)
    app.include_router(job_router)

    @app.middleware("http")
    async def private_responses(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/v1/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/v1/health", tags=["health"])
    def health() -> HealthResponse:
        return HealthResponse()

    @app.get("/v1/ready", tags=["health"])
    async def ready(db: DatabaseDependency) -> HealthResponse:
        if app.state.verifier is None:
            raise DomainError("NOT_READY", "The application is not ready.", 503)
        async with db.transaction("coordinator", None) as connection:
            await connection.execute("select 1")
        return HealthResponse()

    def openapi():
        if app.openapi_schema is None:
            schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
            _, public = models_json_schema(
                [(model, "serialization") for model in PUBLIC_MODELS],
                ref_template="#/components/schemas/{model}",
            )
            schema.setdefault("components", {}).setdefault("schemas", {}).update(public["$defs"])
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi

    return app
