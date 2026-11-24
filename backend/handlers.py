"""Exception handlers: turn every error into Bridle's JSON error envelope."""

from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from errors import BridleError


async def _bridle_error_handler(_: Request, exc: BridleError) -> JSONResponse:
    body: Dict[str, Any] = {"error": {"code": exc.code, "message": exc.message}}
    if exc.details:
        body["error"]["details"] = exc.details
    return JSONResponse(status_code=exc.status_code, content=body)


async def _validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    problems = [
        {"field": ".".join(str(part) for part in err.get("loc", ())), "message": str(err.get("msg", ""))}
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "code": "INVALID_REQUEST",
                "message": "request validation failed",
                "details": {"problems": problems},
            }
        },
    )


async def _unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_ERROR", "message": f"{type(exc).__name__}: {exc}"}},
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach all handlers to the app (same registrations as the original decorators)."""
    app.add_exception_handler(BridleError, _bridle_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)
