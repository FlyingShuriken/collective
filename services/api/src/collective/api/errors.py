from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from collective.api.dto import ErrorResponse


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        self.code = code
        self.message = message
        self.status = status


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_error(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content=ErrorResponse(code=exc.code, message=exc.message).model_dump(mode="json"),
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Validation output must not echo credentials or private request content.
        return JSONResponse(
            status_code=422,
            content=ErrorResponse(
                code="INVALID_REQUEST", message="The request contains invalid or missing fields."
            ).model_dump(mode="json"),
            headers={"Cache-Control": "no-store"},
        )
