"""Results exporter (CSV, JSON, HTML report) and Job State persistence."""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from core.wayback import ArchiveResult

logger = logging.getLogger(__name__)

import os

if os.environ.get("VERCEL"):
    JOBS_DIR = Path("/tmp/data/jobs")
    REPORTS_DIR = Path("/tmp/reports")
else:
    JOBS_DIR = Path(__file__).resolve().parent.parent / "data" / "jobs"
    REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"


@dataclass
class JobState:
    id: str
    website_url: str
    created_at: str
    status: str  # queued, running, paused, completed, failed
    urls_to_archive: list[str]
    results: list[dict[str, Any]]
    current_index: int = 0
    updated_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> JobState:
        return cls(
            id=d["id"],
            website_url=d["website_url"],
            created_at=d["created_at"],
            status=d.get("status", "running"),
            urls_to_archive=d.get("urls_to_archive", []),
            results=d.get("results", []),
            current_index=d.get("current_index", 0),
            updated_at=d.get("updated_at", ""),
        )


def save_job(job: JobState) -> None:
    """Save or update job state in data/jobs/."""
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job.updated_at = datetime.utcnow().isoformat()
    path = JOBS_DIR / f"{job.id}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(job.to_dict(), f, indent=2)


def load_job(job_id: str) -> Optional[JobState]:
    """Load job state by id."""
    path = JOBS_DIR / f"{job_id}.json"
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return JobState.from_dict(data)
    except Exception as e:
        logger.error(f"Error loading job {job_id}: {e}")
        return None


def get_latest_unfinished_job() -> Optional[JobState]:
    """Find the most recent unfinished or paused job to resume."""
    if not JOBS_DIR.exists():
        return None
    files = sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files:
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            job = JobState.from_dict(data)
            if job.status in ("running", "paused") and job.current_index < len(job.urls_to_archive):
                return job
        except Exception:
            continue
    return None


def list_all_jobs() -> list[dict]:
    """List summary of all past jobs."""
    if not JOBS_DIR.exists():
        return []
    jobs = []
    files = sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files:
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            total = len(data.get("urls_to_archive", []))
            res = data.get("results", [])
            success = sum(1 for r in res if r.get("status") == "SUCCESS")
            failed = sum(1 for r in res if r.get("status") == "FAILED")
            skipped = sum(1 for r in res if r.get("status") == "SKIPPED")
            jobs.append({
                "id": data.get("id"),
                "website_url": data.get("website_url"),
                "status": data.get("status"),
                "created_at": data.get("created_at"),
                "total": total,
                "completed": len(res),
                "successful": success,
                "failed": failed,
                "skipped": skipped,
            })
        except Exception:
            continue
    return jobs


def export_csv(results: list[dict], output_path: Path) -> None:
    """Export results to CSV."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["original_url", "status", "archive_url", "capture_timestamp", "error", "http_status"]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow({
                "original_url": r.get("original_url", ""),
                "status": r.get("status", ""),
                "archive_url": r.get("archive_url", ""),
                "capture_timestamp": r.get("capture_timestamp", ""),
                "error": r.get("error", ""),
                "http_status": r.get("http_status", ""),
            })


def export_json(job_state: JobState, output_path: Path) -> None:
    """Export full job data to JSON."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(job_state.to_dict(), f, indent=2)


def generate_html_report(job: JobState, output_path: Path) -> str:
    """Generate a clean, modern HTML report of archiving results."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    results = job.results
    total = len(job.urls_to_archive)
    successful = sum(1 for r in results if r.get("status") == "SUCCESS")
    failed = sum(1 for r in results if r.get("status") == "FAILED")
    skipped = sum(1 for r in results if r.get("status") == "SKIPPED")

    rows_html = []
    for idx, r in enumerate(results, 1):
        status = r.get("status", "")
        badge_cls = "badge-success" if status == "SUCCESS" else ("badge-skipped" if status == "SKIPPED" else "badge-failed")
        archive_url = r.get("archive_url", "")
        action_btn = (
            f'<a href="{archive_url}" target="_blank" rel="noopener" class="btn-snap">Open Snapshot &rarr;</a>'
            if archive_url else '<span class="text-muted">None</span>'
        )
        orig_url = r.get("original_url", "")
        error_msg = r.get("error", "")
        ts = r.get("capture_timestamp", "")

        rows_html.append(f"""
        <tr>
            <td class="text-muted">{idx}</td>
            <td class="cell-url"><a href="{orig_url}" target="_blank" rel="noopener">{orig_url}</a></td>
            <td><span class="badge {badge_cls}">{status}</span></td>
            <td>{action_btn}</td>
            <td class="cell-time">{ts}</td>
            <td class="cell-error">{error_msg}</td>
        </tr>
        """)

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Wayback Archiving Report — {job.website_url}</title>
    <style>
        :root {{
            --bg: #0f172a;
            --surface: #1e293b;
            --border: #334155;
            --text: #f8fafc;
            --text-muted: #94a3b8;
            --accent: #38bdf8;
            --success: #10b981;
            --warning: #f59e0b;
            --danger: #ef4444;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background: var(--bg);
            color: var(--text);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            padding: 32px 24px;
            line-height: 1.5;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
        }}
        header {{
            background: var(--surface);
            padding: 24px 32px;
            border-radius: 12px;
            border: 1px solid var(--border);
            margin-bottom: 24px;
        }}
        h1 {{ font-size: 1.75rem; font-weight: 700; color: #fff; margin-bottom: 8px; }}
        .meta-line {{ color: var(--text-muted); font-size: 0.95rem; }}
        .metrics-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }}
        .metric-card {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 16px 20px;
        }}
        .metric-label {{ font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted); }}
        .metric-value {{ font-size: 1.8rem; font-weight: 700; margin-top: 4px; }}
        .val-total {{ color: var(--accent); }}
        .val-success {{ color: var(--success); }}
        .val-skipped {{ color: var(--warning); }}
        .val-failed {{ color: var(--danger); }}
        .table-wrap {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 12px;
            overflow-x: auto;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            text-align: left;
            font-size: 0.9rem;
        }}
        th {{
            background: #172033;
            color: var(--text-muted);
            padding: 14px 18px;
            font-weight: 600;
            border-bottom: 1px solid var(--border);
        }}
        td {{
            padding: 14px 18px;
            border-bottom: 1px solid var(--border);
            vertical-align: middle;
        }}
        tr:last-child td {{ border-bottom: none; }}
        tr:hover td {{ background: rgba(255, 255, 255, 0.02); }}
        .badge {{
            display: inline-block;
            padding: 4px 10px;
            border-radius: 6px;
            font-weight: 600;
            font-size: 0.75rem;
            text-transform: uppercase;
        }}
        .badge-success {{ background: rgba(16, 185, 129, 0.15); color: var(--success); border: 1px solid rgba(16, 185, 129, 0.3); }}
        .badge-skipped {{ background: rgba(245, 158, 11, 0.15); color: var(--warning); border: 1px solid rgba(245, 158, 11, 0.3); }}
        .badge-failed {{ background: rgba(239, 68, 68, 0.15); color: var(--danger); border: 1px solid rgba(239, 68, 68, 0.3); }}
        .btn-snap {{
            display: inline-block;
            background: #2563eb;
            color: #fff;
            text-decoration: none;
            padding: 6px 12px;
            border-radius: 6px;
            font-size: 0.8rem;
            font-weight: 600;
            transition: background 0.15s;
        }}
        .btn-snap:hover {{ background: #1d4ed8; }}
        a {{ color: var(--accent); text-decoration: none; word-break: break-all; }}
        a:hover {{ text-decoration: underline; }}
        .cell-url {{ max-width: 380px; }}
        .cell-time {{ color: var(--text-muted); font-size: 0.82rem; white-space: nowrap; }}
        .cell-error {{ color: #f87171; font-size: 0.82rem; }}
        .text-muted {{ color: var(--text-muted); }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>Wayback Archive Report</h1>
            <div class="meta-line">Website: <strong><a href="{job.website_url}" target="_blank">{job.website_url}</a></strong></div>
            <div class="meta-line">Generated on: {datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")} | Job ID: {job.id}</div>
        </header>

        <div class="metrics-grid">
            <div class="metric-card">
                <div class="metric-label">Total URLs</div>
                <div class="metric-value val-total">{total}</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Archived Successfully</div>
                <div class="metric-value val-success">{successful}</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Skipped (Recent)</div>
                <div class="metric-value val-skipped">{skipped}</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Failed</div>
                <div class="metric-value val-failed">{failed}</div>
            </div>
        </div>

        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th style="width: 40px">#</th>
                        <th>Original Page URL</th>
                        <th style="width: 110px">Status</th>
                        <th style="width: 160px">Wayback Snapshot</th>
                        <th style="width: 170px">Timestamp</th>
                        <th>Notes / Error</th>
                    </tr>
                </thead>
                <tbody>
                    {"".join(rows_html)}
                </tbody>
            </table>
        </div>
    </div>
</body>
</html>
"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    return str(output_path)
