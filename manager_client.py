import logging
import httpx
from config import settings

logger = logging.getLogger("maintenance-agent.manager")


async def heartbeat(status: str = "active", metrics: dict = None):
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{settings.MANAGER_URL}/api/heartbeat",
                json={"agent_name": settings.AGENT_NAME, "status": status, "metrics": metrics or {}},
                timeout=5
            )
    except Exception as e:
        logger.error(f"Heartbeat failed: {e}")


async def create_task(title: str, task_type: str, input_data: dict = None, priority: str = "normal"):
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{settings.MANAGER_URL}/api/tasks",
                json={"agent_id": 4, "title": title, "task_type": task_type, "priority": priority, "input_data": input_data or {}},
                timeout=5
            )
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        logger.error(f"Create task failed: {e}")
    return None


async def update_task(task_id: int, status: str, output_data: dict = None, error_message: str = None):
    try:
        async with httpx.AsyncClient() as client:
            payload = {"status": status}
            if output_data:
                payload["output_data"] = output_data
            if error_message:
                payload["error_message"] = error_message
            await client.patch(
                f"{settings.MANAGER_URL}/api/tasks/{task_id}",
                json=payload,
                timeout=5
            )
    except Exception as e:
        logger.error(f"Update task failed: {e}")


async def update_kpi(kpi_name: str, value: float):
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{settings.MANAGER_URL}/api/kpis", timeout=5)
            if resp.status_code == 200:
                kpis = resp.json()
                for kpi in kpis:
                    if kpi["name"] == kpi_name:
                        await client.post(
                            f"{settings.MANAGER_URL}/api/kpis/{kpi['id']}/record",
                            json={"value": value},
                            timeout=5
                        )
                        return
    except Exception as e:
        logger.error(f"Update KPI failed: {e}")


async def log_message(level: str, message: str):
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{settings.MANAGER_URL}/api/logs",
                params={"agent_name": settings.AGENT_NAME, "level": level, "message": message},
                timeout=5
            )
    except Exception:
        pass


async def send_alert(title: str, message: str, severity: str = "info"):
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{settings.MANAGER_URL}/api/alerts/test",
                timeout=5
            )
    except Exception:
        pass
