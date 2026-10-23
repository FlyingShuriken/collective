from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from pydantic.json_schema import models_json_schema

from collective.api.auth import JWTVerifier
from collective.api.dto import PUBLIC_MODELS, HealthResponse
from collective.api.errors import install_error_handlers
from collective.repositories.db import Database
from collective.settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        db = Database(settings.database_url.get_secret_value()) if settings.database_url else None
        app.state.database = db
        try:
            if db is not None:
                await db.open()
            yield
        finally:
            if db is not None:
                await db.close()

    app = FastAPI(title="Orbyte Collective", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.database = None
    app.state.verifier = JWTVerifier(settings) if settings.supabase_url else None
    install_error_handlers(app)

    @app.get("/v1/health", tags=["health"])
    def health() -> HealthResponse:
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
