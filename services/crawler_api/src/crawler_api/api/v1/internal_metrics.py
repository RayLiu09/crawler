import logging

from fastapi import APIRouter, HTTPException, Request, Response, status

logger = logging.getLogger(__name__)

router = APIRouter(tags=["internal"], include_in_schema=False)


@router.get("/internal/metrics")
async def metrics(request: Request) -> Response:
    exporter = getattr(request.app.state, "webhook_metrics", None)
    if exporter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        payload = await exporter.render_latest()
    except Exception as error:
        logger.error(
            "webhook metrics snapshot failed",
            extra={"error_type": type(error).__name__},
        )
        return Response(
            content=b"metrics unavailable\n",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )
    return Response(
        content=payload,
        headers={"Content-Type": exporter.content_type},
    )
