"""Wayback Website Archiver — FastAPI Desktop Web Application."""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.crawler import WebsiteCrawler
from core.results import (
    JobState,
    export_csv,
    export_json,
    generate_html_report,
    get_latest_unfinished_job,
    list_all_jobs,
    load_job,
    save_job,
    JOBS_DIR,
    REPORTS_DIR,
)
from core.settings import Settings, load_settings, save_settings
from core.wayback import WaybackClient

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("archiver_app")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Wayback Website Archiver", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# In-memory tracking for active discovery and archiving tasks
discovery_tasks: dict[str, dict[str, Any]] = {}
active_jobs: dict[str, dict[str, Any]] = {}
job_locks: dict[str, threading.Event] = {}
cancel_flags: dict[str, bool] = {}


# --- Pydantic Request Models ---
class DiscoverRequest(BaseModel):
    url: str
    mode: str = "smart"  # smart, sitemap, menu
    max_pages: int = 500
    max_depth: int = 3
    include_external: bool = False


class ArchiveRequest(BaseModel):
    website_url: str
    urls: list[str]
    archive_delay: Optional[int] = None
    skip_existing_days: Optional[int] = None
    check_existing_snapshots: Optional[bool] = None


class SettingsRequest(BaseModel):
    access_key: str
    secret_key: str
    max_pages: int = 500
    max_depth: int = 3
    archive_delay: int = 10
    request_timeout: int = 60
    retries: int = 3
    check_existing_snapshots: bool = True
    skip_existing_days: int = 30
    include_external: bool = False


class TestConnectionRequest(BaseModel):
    access_key: Optional[str] = None
    secret_key: Optional[str] = None


# --- HTML Page Routes ---
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_file = TEMPLATES_DIR / "index.html"
    return HTMLResponse(content=index_file.read_text(encoding="utf-8"))


@app.get("/settings", response_class=HTMLResponse)
async def serve_settings():
    settings_file = TEMPLATES_DIR / "settings.html"
    return HTMLResponse(content=settings_file.read_text(encoding="utf-8"))


@app.get("/results", response_class=HTMLResponse)
async def serve_results():
    results_file = TEMPLATES_DIR / "results.html"
    return HTMLResponse(content=results_file.read_text(encoding="utf-8"))


# --- Settings API Routes ---
@app.get("/api/settings")
async def get_settings():
    settings = load_settings()
    return settings.to_safe_dict()


@app.post("/api/settings")
async def update_settings(req: SettingsRequest):
    settings = load_settings()
    # Only update secret key if not masked or not empty
    if req.secret_key and "****" not in req.secret_key:
        settings.secret_key = req.secret_key
    settings.access_key = req.access_key
    settings.max_pages = req.max_pages
    settings.max_depth = req.max_depth
    settings.archive_delay = req.archive_delay
    settings.request_timeout = req.request_timeout
    settings.retries = req.retries
    settings.check_existing_snapshots = req.check_existing_snapshots
    settings.skip_existing_days = req.skip_existing_days
    settings.include_external = req.include_external
    save_settings(settings)
    return {"status": "ok", "message": "Settings saved successfully."}


@app.post("/api/settings/test")
async def test_settings_connection(req: TestConnectionRequest):
    settings = load_settings()
    access = req.access_key or settings.access_key
    secret = req.secret_key or settings.secret_key

    client = WaybackClient(access_key=access, secret_key=secret)
    success, message = client.test_connection()
    return {"success": success, "message": message}


# --- Discovery API Routes ---
def _run_discovery_worker(task_id: str, req: DiscoverRequest):
    task_data = discovery_tasks[task_id]
    try:
        def on_crawler_update(info: dict):
            if info.get("type") == "log":
                task_data["latest_log"] = info.get("message", "")
            elif info.get("type") == "url_found":
                task_data["count"] = info.get("count", 0)

        crawler = WebsiteCrawler(
            base_url=req.url,
            max_pages=req.max_pages,
            max_depth=req.max_depth,
            include_external=req.include_external,
            status_callback=on_crawler_update,
        )

        task_data["status"] = "running"
        if req.mode == "sitemap":
            urls = crawler.crawl_sitemap_only()
        elif req.mode == "menu":
            urls = crawler.crawl_menu_only()
        else:
            urls = crawler.crawl_smart()

        task_data["status"] = "completed"
        task_data["urls"] = urls
        task_data["count"] = len(urls)
        task_data["latest_log"] = f"Finished. Discovered {len(urls)} unique pages."
    except Exception as e:
        logger.exception("Discovery failed")
        task_data["status"] = "failed"
        task_data["error"] = str(e)


@app.post("/api/discover")
async def start_discovery(req: DiscoverRequest):
    task_id = str(uuid.uuid4())[:8]
    discovery_tasks[task_id] = {
        "task_id": task_id,
        "website_url": req.url,
        "mode": req.mode,
        "status": "starting",
        "latest_log": "Starting discovery crawler...",
        "count": 0,
        "urls": [],
        "error": "",
    }
    thread = threading.Thread(target=_run_discovery_worker, args=(task_id, req), daemon=True)
    thread.start()
    return {"task_id": task_id}


@app.get("/api/discover/status/{task_id}")
async def get_discovery_status(task_id: str):
    if task_id not in discovery_tasks:
        raise HTTPException(status_code=404, detail="Discovery task not found")
    return discovery_tasks[task_id]


# --- Archiving API Routes ---
def _run_archiving_worker(job_id: str):
    job = load_job(job_id)
    if not job:
        logger.error(f"Cannot run worker: Job {job_id} not found")
        return

    settings = load_settings()
    client = WaybackClient(
        access_key=settings.access_key,
        secret_key=settings.secret_key,
        request_timeout=settings.request_timeout,
        retries=settings.retries,
        archive_delay=settings.archive_delay,
    )

    pause_event = job_locks.get(job_id)
    if not pause_event:
        pause_event = threading.Event()
        pause_event.set()
        job_locks[job_id] = pause_event

    job_meta = active_jobs.setdefault(job_id, {
        "current_url": "",
        "current_stage": "Starting queue...",
    })

    try:
        job.status = "running"
        save_job(job)

        while job.current_index < len(job.urls_to_archive):
            # Check cancellation
            if cancel_flags.get(job_id):
                job.status = "cancelled"
                save_job(job)
                job_meta["current_stage"] = "Cancelled by user"
                return

            # Check pause
            pause_event.wait()

            url = job.urls_to_archive[job.current_index]
            job_meta["current_url"] = url
            job_meta["current_stage"] = f"Processing ({job.current_index + 1}/{len(job.urls_to_archive)})..."

            def notify_stage(stage_msg: str):
                job_meta["current_stage"] = stage_msg

            # Execute capture
            result = client.archive_url(
                url=url,
                check_existing=settings.check_existing_snapshots,
                skip_existing_days=settings.skip_existing_days,
                status_callback=notify_stage,
            )

            job.results.append(asdict(result))
            job.current_index += 1
            save_job(job)

            # Polite delay between items if there are more URLs
            if job.current_index < len(job.urls_to_archive):
                delay = settings.archive_delay
                for s in range(delay, 0, -1):
                    if cancel_flags.get(job_id):
                        break
                    job_meta["current_stage"] = f"Waiting {s}s before next URL (rate limiting safety)..."
                    time.sleep(1)

        job.status = "completed"
        save_job(job)
        job_meta["current_stage"] = "Archiving completed successfully!"

        # Generate exported reports
        report_dir = REPORTS_DIR / job_id
        export_csv(job.results, report_dir / "results.csv")
        export_json(job, report_dir / "results.json")
        generate_html_report(job, report_dir / "archive_report.html")

    except Exception as e:
        logger.exception(f"Archiving job {job_id} encountered an error")
        job.status = "failed"
        save_job(job)
        job_meta["current_stage"] = f"Job failed: {e}"


@app.post("/api/archive")
async def start_archive(req: ArchiveRequest):
    if not req.urls:
        raise HTTPException(status_code=400, detail="No URLs provided to archive")

    settings = load_settings()
    if req.archive_delay is not None:
        settings.archive_delay = req.archive_delay
    if req.skip_existing_days is not None:
        settings.skip_existing_days = req.skip_existing_days
    save_settings(settings)

    job_id = str(uuid.uuid4())[:8]
    job = JobState(
        id=job_id,
        website_url=req.website_url,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        status="running",
        urls_to_archive=req.urls,
        results=[],
        current_index=0,
    )
    save_job(job)

    # Initialize pause/cancel signals
    pause_event = threading.Event()
    pause_event.set()
    job_locks[job_id] = pause_event
    cancel_flags[job_id] = False

    active_jobs[job_id] = {
        "current_url": "",
        "current_stage": "Starting archive thread...",
    }

    thread = threading.Thread(target=_run_archiving_worker, args=(job_id,), daemon=True)
    thread.start()
    return {"job_id": job_id}


@app.get("/api/archive/status/{job_id}")
async def get_archive_status(job_id: str):
    job = load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    meta = active_jobs.get(job_id, {})
    return {
        "id": job.id,
        "website_url": job.website_url,
        "status": job.status,
        "total": len(job.urls_to_archive),
        "completed_count": len(job.results),
        "current_index": job.current_index,
        "current_url": meta.get("current_url", ""),
        "current_stage": meta.get("current_stage", ""),
        "results": job.results,
    }


@app.post("/api/archive/pause/{job_id}")
async def pause_archive(job_id: str):
    job = load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    event = job_locks.get(job_id)
    if event:
        event.clear()
    job.status = "paused"
    save_job(job)
    return {"status": "paused"}


@app.post("/api/archive/resume/{job_id}")
async def resume_archive_control(job_id: str):
    job = load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    event = job_locks.get(job_id)
    if event:
        event.set()
    else:
        new_event = threading.Event()
        new_event.set()
        job_locks[job_id] = new_event
        thread = threading.Thread(target=_run_archiving_worker, args=(job_id,), daemon=True)
        thread.start()

    job.status = "running"
    save_job(job)
    return {"status": "resumed"}


@app.post("/api/archive/cancel/{job_id}")
async def cancel_archive(job_id: str):
    cancel_flags[job_id] = True
    event = job_locks.get(job_id)
    if event:
        event.set()
    job = load_job(job_id)
    if job:
        job.status = "cancelled"
        save_job(job)
    return {"status": "cancelled"}


@app.get("/api/jobs")
async def get_all_jobs():
    return list_all_jobs()


@app.get("/api/jobs/unfinished")
async def get_unfinished():
    job = get_latest_unfinished_job()
    if not job:
        return {"job": None}
    return {"job": job.to_dict()}


@app.post("/api/jobs/{job_id}/resume")
async def resume_unfinished_job(job_id: str):
    job = load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    cancel_flags[job_id] = False
    pause_event = threading.Event()
    pause_event.set()
    job_locks[job_id] = pause_event

    thread = threading.Thread(target=_run_archiving_worker, args=(job_id,), daemon=True)
    thread.start()
    return {"status": "resumed", "job_id": job_id}


# --- Exports and Reports Routes ---
@app.get("/api/jobs/{job_id}/export/csv")
async def download_csv(job_id: str):
    job = load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    csv_path = REPORTS_DIR / job_id / "results.csv"
    export_csv(job.results, csv_path)
    return FileResponse(
        csv_path,
        media_type="text/csv",
        filename=f"archive_results_{job_id}.csv",
    )


@app.get("/api/jobs/{job_id}/export/json")
async def download_json(job_id: str):
    job = load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    json_path = REPORTS_DIR / job_id / "results.json"
    export_json(job, json_path)
    return FileResponse(
        json_path,
        media_type="application/json",
        filename=f"archive_results_{job_id}.json",
    )


@app.get("/api/jobs/{job_id}/report", response_class=HTMLResponse)
async def view_report(job_id: str):
    job = load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    report_path = REPORTS_DIR / job_id / "archive_report.html"
    generate_html_report(job, report_path)
    return HTMLResponse(content=report_path.read_text(encoding="utf-8"))


def open_browser():
    """Wait briefly for server to start, then launch browser."""
    time.sleep(1.2)
    webbrowser.open("http://127.0.0.1:8000")


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("      WAYBACK WEBSITE ARCHIVER — DESKTOP SERVER")
    print("=" * 60)
    print("Opening web interface at: http://127.0.0.1:8000")
    print("Press Ctrl+C to stop the server.\n")

    threading.Thread(target=open_browser, daemon=True).start()
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)
