from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/scheduler-status")
async def scheduler_status(request: Request) -> dict:
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
