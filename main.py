"""
PetHub Site Maintenance Agent - Main FastAPI Application
Handles broken link scanning, duplicate detection, performance monitoring,
metadata auditing, content auditing, and security scanning.
"""

import json
import os
import logging
import asyncio
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import settings
from manager_client import heartbeat, create_task, update_task, log_message
from link_checker import scan_all_links, fix_broken_links
from duplicate_detector import scan_for_duplicates
from performance_monitor import scan_all_performance
from metadata_auditor import audit_all_metadata
from content_auditor import audit_content
from security_checker import run_security_scan

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("maintenance-agent")

scheduler = AsyncIOScheduler()

# ─── In-memory state (persisted to JSON) ────────────────────────────────────

state = {
    "last_link_scan": None,
    "last_duplicate_scan": None,
    "last_performance_scan": None,
    "last_metadata_audit": None,
    "last_content_audit": None,
    "last_security_scan": None,
    "scan_history": [],
    "started_at": None,
    "errors": [],
}


def load_state():
    os.makedirs(os.path.dirname(settings.DB_PATH), exist_ok=True)
    if os.path.exists(settings.DB_PATH):
        try:
            with open(settings.DB_PATH, "r") as f:
                data = json.load(f)
                for key in state:
                    if key in data:
                        state[key] = data[key]
                state["errors"] = state["errors"][-50:]
                state["scan_history"] = state["scan_history"][-100:]
                logger.info("Loaded persisted state")
        except Exception as e:
            logger.error(f"Failed to load state: {e}")


def save_state():
    os.makedirs(os.path.dirname(settings.DB_PATH), exist_ok=True)
    try:
        with open(settings.DB_PATH, "w") as f:
            json.dump(
                {
                    "last_link_scan": state["last_link_scan"],
                    "last_duplicate_scan": state["last_duplicate_scan"],
                    "last_performance_scan": state["last_performance_scan"],
                    "last_metadata_audit": state["last_metadata_audit"],
                    "last_content_audit": state["last_content_audit"],
                    "last_security_scan": state["last_security_scan"],
                    "scan_history": state["scan_history"][-100:],
                    "errors": state["errors"][-50:],
                },
                f,
                default=str,
            )
    except Exception as e:
        logger.error(f"Failed to save state: {e}")


def add_error(msg: str):
    state["errors"].append({
        "message": msg,
        "time": datetime.now(timezone.utc).isoformat(),
    })
    state["errors"] = state["errors"][-50:]


def record_scan(scan_type: str, summary: str):
    state["scan_history"].append({
        "type": scan_type,
        "summary": summary,
        "time": datetime.now(timezone.utc).isoformat(),
    })
    state["scan_history"] = state["scan_history"][-100:]


# ─── Scheduled scan wrappers ────────────────────────────────────────────────

async def scheduled_link_scan():
    logger.info("Running scheduled link scan...")
    try:
        result = await scan_all_links()
        state["last_link_scan"] = result
        record_scan("links", f"Checked {result['total_links_checked']} links, {result['broken_count']} broken")
        save_state()
        await log_message("info", f"Link scan: {result['total_links_checked']} checked, {result['broken_count']} broken")
    except Exception as e:
        logger.error(f"Link scan failed: {e}")
        add_error(f"Link scan failed: {e}")
        save_state()


async def scheduled_duplicate_scan():
    logger.info("Running scheduled duplicate scan...")
    try:
        result = await scan_for_duplicates()
        state["last_duplicate_scan"] = result
        record_scan("duplicates", f"Scanned {result['total_pages']} pages, {result['duplicate_pairs']} duplicate pairs")
        save_state()
        await log_message("info", f"Duplicate scan: {result['duplicate_pairs']} pairs found")
    except Exception as e:
        logger.error(f"Duplicate scan failed: {e}")
        add_error(f"Duplicate scan failed: {e}")
        save_state()


async def scheduled_performance_scan():
    logger.info("Running scheduled performance scan...")
    try:
        result = await scan_all_performance()
        state["last_performance_scan"] = result
        record_scan("performance", f"Scanned {result['total_pages']} pages, avg TTFB {result['avg_ttfb_ms']}ms")
        save_state()
        await log_message("info", f"Performance scan: {result['total_pages']} pages, avg TTFB {result['avg_ttfb_ms']}ms")
    except Exception as e:
        logger.error(f"Performance scan failed: {e}")
        add_error(f"Performance scan failed: {e}")
        save_state()


async def scheduled_metadata_audit():
    logger.info("Running scheduled metadata audit...")
    try:
        result = await audit_all_metadata()
        state["last_metadata_audit"] = result
        record_scan("metadata", f"Audited {result['total_pages']} pages, {result['total_issues']} issues")
        save_state()
        await log_message("info", f"Metadata audit: {result['total_issues']} issues across {result['total_pages']} pages")
    except Exception as e:
        logger.error(f"Metadata audit failed: {e}")
        add_error(f"Metadata audit failed: {e}")
        save_state()


async def scheduled_content_audit():
    logger.info("Running scheduled content audit...")
    try:
        result = await audit_content()
        state["last_content_audit"] = result
        recs = len(result.get("recommendations", []))
        record_scan("content", f"Audited {result['total_pages']} pages, avg health {result['avg_health_score']}, {recs} recommendations")
        save_state()
        await log_message("info", f"Content audit: avg health {result['avg_health_score']}, {recs} recommendations")
    except Exception as e:
        logger.error(f"Content audit failed: {e}")
        add_error(f"Content audit failed: {e}")
        save_state()


async def scheduled_security_scan():
    logger.info("Running scheduled security scan...")
    try:
        result = await run_security_scan()
        state["last_security_scan"] = result
        record_scan("security", f"Grade: {result['grade']}, {result['total_findings']} findings")
        save_state()
        await log_message("info", f"Security scan: grade {result['grade']}, {result['total_findings']} findings")
    except Exception as e:
        logger.error(f"Security scan failed: {e}")
        add_error(f"Security scan failed: {e}")
        save_state()


async def send_heartbeat():
    scans_done = len(state["scan_history"])
    errors = len(state["errors"])
    await heartbeat("active", {
        "tasks_completed": scans_done,
        "tasks_failed": errors,
        "avg_latency_ms": 0,
    })


# ─── App lifecycle ──────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    load_state()
    state["started_at"] = datetime.now(timezone.utc).isoformat()

    # Heartbeat
    scheduler.add_job(send_heartbeat, "interval", seconds=settings.HEARTBEAT_INTERVAL, id="heartbeat")

    # Link scan: every 2 days (Mon/Wed/Fri/Sun 6am UTC)
    scheduler.add_job(scheduled_link_scan, CronTrigger(day_of_week="mon,wed,fri,sun", hour=6, minute=0), id="link_scan")

    # Duplicate detection: every 3 days (Mon/Thu 7am UTC)
    scheduler.add_job(scheduled_duplicate_scan, CronTrigger(day_of_week="mon,thu", hour=7, minute=0), id="duplicate_scan")

    # Performance monitoring: daily 5am UTC
    scheduler.add_job(scheduled_performance_scan, CronTrigger(hour=5, minute=0), id="performance_scan")

    # Metadata audit: every 2 days (Mon/Wed/Fri 3am UTC)
    scheduler.add_job(scheduled_metadata_audit, CronTrigger(day_of_week="mon,wed,fri", hour=3, minute=0), id="metadata_audit")

    # Content audit: every 3 days (Tue/Fri 4am UTC)
    scheduler.add_job(scheduled_content_audit, CronTrigger(day_of_week="tue,fri", hour=4, minute=0), id="content_audit")

    # Security scan: every 3 days (Mon/Thu/Sun 3am UTC)
    scheduler.add_job(scheduled_security_scan, CronTrigger(day_of_week="mon,thu,sun", hour=3, minute=0), id="security_scan")

    scheduler.start()
    await send_heartbeat()
    await log_message("info", "Maintenance Agent started")
    logger.info("Maintenance Agent started on port %d", settings.API_PORT)
    yield
    scheduler.shutdown()


app = FastAPI(
    title="PetHub Site Maintenance Agent",
    description="Automated site maintenance, link checking, performance monitoring, and security scanning for pethubonline.com",
    version="1.0.0",
    lifespan=lifespan,
    root_path="/agents/maintenance",
)


# ─── API Endpoints ──────────────────────────────────────────────────────────

@app.get("/api/status")
async def get_status():
    now = datetime.now(timezone.utc)
    uptime = None
    if state["started_at"]:
        try:
            started = datetime.fromisoformat(state["started_at"])
            uptime = str(now - started).split(".")[0]
        except Exception:
            pass

    return {
        "agent": "maintenance",
        "status": "active",
        "uptime": uptime,
        "started_at": state["started_at"],
        "last_link_scan": state["last_link_scan"].get("scanned_at") if state["last_link_scan"] else None,
        "last_duplicate_scan": state["last_duplicate_scan"].get("scanned_at") if state["last_duplicate_scan"] else None,
        "last_performance_scan": state["last_performance_scan"].get("scanned_at") if state["last_performance_scan"] else None,
        "last_metadata_audit": state["last_metadata_audit"].get("scanned_at") if state["last_metadata_audit"] else None,
        "last_content_audit": state["last_content_audit"].get("scanned_at") if state["last_content_audit"] else None,
        "last_security_scan": state["last_security_scan"].get("scanned_at") if state["last_security_scan"] else None,
        "total_scans": len(state["scan_history"]),
        "recent_errors": len(state["errors"]),
    }


# --- Links ---

@app.post("/api/links/scan")
async def trigger_link_scan():
    asyncio.create_task(scheduled_link_scan())
    return {"message": "Link scan started", "status": "running"}


@app.get("/api/links/report")
async def get_link_report():
    if not state["last_link_scan"]:
        return {"message": "No link scan results yet. Trigger a scan first."}
    return state["last_link_scan"]


@app.post("/api/links/fix")
async def trigger_link_fix():
    if not state["last_link_scan"] or not state["last_link_scan"].get("broken_links"):
        raise HTTPException(400, "No broken links to fix. Run a link scan first.")
    try:
        result = await fix_broken_links(state["last_link_scan"]["broken_links"])
        return result
    except Exception as e:
        raise HTTPException(500, f"Fix failed: {e}")


# --- Duplicates ---

@app.post("/api/duplicates/scan")
async def trigger_duplicate_scan():
    asyncio.create_task(scheduled_duplicate_scan())
    return {"message": "Duplicate scan started", "status": "running"}


@app.get("/api/duplicates")
async def get_duplicates():
    if not state["last_duplicate_scan"]:
        return {"message": "No duplicate scan results yet. Trigger a scan first."}
    return state["last_duplicate_scan"]


# --- Performance ---

@app.post("/api/performance/scan")
async def trigger_performance_scan():
    asyncio.create_task(scheduled_performance_scan())
    return {"message": "Performance scan started", "status": "running"}


@app.get("/api/performance")
async def get_performance():
    if not state["last_performance_scan"]:
        return {"message": "No performance scan results yet. Trigger a scan first."}
    return state["last_performance_scan"]


# --- Metadata ---

@app.post("/api/metadata/scan")
async def trigger_metadata_scan():
    asyncio.create_task(scheduled_metadata_audit())
    return {"message": "Metadata audit started", "status": "running"}


@app.get("/api/metadata")
async def get_metadata():
    if not state["last_metadata_audit"]:
        return {"message": "No metadata audit results yet. Trigger a scan first."}
    return state["last_metadata_audit"]


# --- Content ---

@app.post("/api/content/scan")
async def trigger_content_scan():
    asyncio.create_task(scheduled_content_audit())
    return {"message": "Content audit started", "status": "running"}


@app.get("/api/content")
async def get_content():
    if not state["last_content_audit"]:
        return {"message": "No content audit results yet. Trigger a scan first."}
    return state["last_content_audit"]


# --- Security ---

@app.post("/api/security/scan")
async def trigger_security_scan():
    asyncio.create_task(scheduled_security_scan())
    return {"message": "Security scan started", "status": "running"}


@app.get("/api/security")
async def get_security():
    if not state["last_security_scan"]:
        return {"message": "No security scan results yet. Trigger a scan first."}
    return state["last_security_scan"]


# --- Overview ---

@app.get("/api/overview")
async def get_overview():
    """Combined overview of all scans for dashboard summary."""
    link_data = state["last_link_scan"] or {}
    dup_data = state["last_duplicate_scan"] or {}
    perf_data = state["last_performance_scan"] or {}
    meta_data = state["last_metadata_audit"] or {}
    content_data = state["last_content_audit"] or {}
    sec_data = state["last_security_scan"] or {}

    return {
        "links": {
            "scanned_at": link_data.get("scanned_at"),
            "total_checked": link_data.get("total_links_checked", 0),
            "broken_count": link_data.get("broken_count", 0),
        },
        "duplicates": {
            "scanned_at": dup_data.get("scanned_at"),
            "total_pages": dup_data.get("total_pages", 0),
            "duplicate_pairs": dup_data.get("duplicate_pairs", 0),
        },
        "performance": {
            "scanned_at": perf_data.get("scanned_at"),
            "total_pages": perf_data.get("total_pages", 0),
            "avg_ttfb_ms": perf_data.get("avg_ttfb_ms", 0),
            "grades": perf_data.get("grades", {}),
        },
        "metadata": {
            "scanned_at": meta_data.get("scanned_at"),
            "total_pages": meta_data.get("total_pages", 0),
            "total_issues": meta_data.get("total_issues", 0),
            "consistency_score": meta_data.get("consistency_score", 0),
        },
        "content": {
            "scanned_at": content_data.get("scanned_at"),
            "total_pages": content_data.get("total_pages", 0),
            "avg_health_score": content_data.get("avg_health_score", 0),
            "recommendations": len(content_data.get("recommendations", [])),
        },
        "security": {
            "scanned_at": sec_data.get("scanned_at"),
            "grade": sec_data.get("grade", "-"),
            "severity_counts": sec_data.get("severity_counts", {}),
        },
    }


# --- History ---

@app.get("/api/history")
async def get_history():
    history = list(reversed(state["scan_history"]))
    return {"total": len(history), "history": history}


@app.get("/api/errors")
async def get_errors():
    return {"errors": state["errors"][-20:]}


# --- Dashboard ---

@app.get("/", response_class=HTMLResponse)
async def maintenance_dashboard():
    with open("templates/maintenance_dashboard.html", "r") as f:
        return HTMLResponse(f.read())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=settings.API_PORT, reload=False)
