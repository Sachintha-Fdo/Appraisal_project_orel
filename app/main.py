import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes_appraisal import public_router, router
from app.config import get_settings
from app.container import build_container, validate_settings

logger = logging.getLogger("app")


def create_app() -> FastAPI:
    application = FastAPI(
        title="Orel Server — CDB Appraisal PII Gateway",
        version="0.4.0",
        description=(
            "On-prem masking (Presidio + AES-256-GCM + Redis), OCR PDF line masking. "
            "Forwards masked requests to CDB Local Server, the only component with "
            "real Azure network access, and unmasks whatever it relays back."
        ),
    )

    @application.on_event("startup")
    def _startup() -> None:
        settings = get_settings()
        
        logging.basicConfig(
            level=getattr(logging, settings.log_level.upper(), logging.INFO),
            format="%(asctime)s [%(levelname)8s] %(name)s: %(message)s",
        )
        validate_settings(settings)
        container = build_container(settings)
        
        container.postgres.init_schema()
        application.state.container = container

    @application.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        
        logger.exception("Unhandled exception on %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    application.include_router(router)
    application.include_router(public_router)
    return application


app = create_app()
