"""Scheduler status route.

Provides runtime scheduler metadata for operational visibility, including
whether the scheduler is running and the next run time for each registered job.
"""

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/scheduler-status")
async def scheduler_status(request: Request) -> dict:
    """Return scheduler runtime state and job metadata.
    Args:
        request: FastAPI request object with app state; expects
            ``request.app.state.scheduler`` to be initialized at startup.
    Returns:
        dict: JSON-serializable structure with:
            - ``running`` (bool): scheduler running flag.
            - ``jobs`` (list[dict]): id, name, and optional next_run string.
    """
    s = request.app.state.scheduler
    jobs = s.get_jobs()
    return {
        "running": s.running,
        "jobs": [
            {
                "id": j.id,
                "name": j.name,
                "next_run": str(j.next_run_time) if j.next_run_time else None,
            }
            for j in jobs
        ],
    }
