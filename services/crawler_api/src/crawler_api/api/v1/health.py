from fastapi import APIRouter, Request, Response, status

from crawler_api.infrastructure.database.health import database_is_ready

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(request: Request, response: Response) -> dict[str, str]:
    if not await database_is_ready(request.app.state.database_engine):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready"}
    return {"status": "ok"}
