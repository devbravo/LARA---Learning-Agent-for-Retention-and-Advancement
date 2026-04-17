"""Health check route.
Exposes a lightweight liveness endpoint used by uptime probes.
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health():
    """Return basic service liveness status.
    Returns:
        dict: ``{"status": "ok"}`` when the API process is running.
    """
    return {"status": "ok"}
