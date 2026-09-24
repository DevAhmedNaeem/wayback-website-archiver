"""Wayback Machine Save Page Now (SPN2) and Availability client."""

from __future__ import annotations

import datetime
import logging
import time
from dataclasses import dataclass
from typing import Optional, Callable
import requests

logger = logging.getLogger(__name__)

SAVE_API_URL = "https://web.archive.org/save/"
STATUS_API_URL = "https://web.archive.org/save/status/"
AVAILABILITY_API_URL = "https://archive.org/wayback/available"
USER_STATUS_API_URL = "https://web.archive.org/save/status/user"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


@dataclass
class ArchiveResult:
    original_url: str
    status: str  # SUCCESS, FAILED, SKIPPED
    archive_url: str = ""
    capture_timestamp: str = ""
    error: str = ""
    http_status: Optional[int] = None
    existing: bool = False


class WaybackClient:
    """Client for Internet Archive Wayback Machine Save Page Now API."""

    def __init__(
        self,
        access_key: str = "",
        secret_key: str = "",
        request_timeout: int = 60,
        retries: int = 3,
        archive_delay: int = 10,
    ):
        self.access_key = access_key.strip()
        self.secret_key = secret_key.strip()
        self.request_timeout = request_timeout
        self.retries = retries
        self.archive_delay = archive_delay

        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        if self.has_auth():
            self.session.headers["Authorization"] = f"LOW {self.access_key}:{self.secret_key}"

    def has_auth(self) -> bool:
        return bool(self.access_key and self.secret_key)

    def test_connection(self) -> tuple[bool, str]:
        """Test API credentials with Save Page Now status endpoint."""
        if not self.has_auth():
            return False, "Access key or Secret key is missing."

        try:
            resp = self.session.get(USER_STATUS_API_URL, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                avail = data.get("available", "unknown")
                proc = data.get("processing", "0")
                return True, f"Connection successful! Available slots: {avail}, Processing: {proc}"
            elif resp.status_code in (401, 403):
                return False, f"Authentication failed (HTTP {resp.status_code}). Please verify your Access and Secret keys."
            else:
                return False, f"Unexpected response from Wayback API (HTTP {resp.status_code}): {resp.text[:200]}"
        except Exception as e:
            return False, f"Network error testing Wayback connection: {e}"

    def check_existing_snapshot(self, url: str) -> tuple[bool, str, Optional[datetime.datetime]]:
        """Check if an existing snapshot exists via Wayback Availability API.
        
        Returns (exists, snapshot_url, timestamp_dt).
        """
        try:
            resp = requests.get(
                AVAILABILITY_API_URL,
                params={"url": url},
                headers=DEFAULT_HEADERS,
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                closest = data.get("archived_snapshots", {}).get("closest")
                if closest and closest.get("available"):
                    snap_url = closest.get("url", "")
                    ts_str = closest.get("timestamp", "")
                    dt: Optional[datetime.datetime] = None
                    if ts_str and len(ts_str) >= 8:
                        try:
                            dt = datetime.datetime.strptime(ts_str[:14].ljust(14, "0"), "%Y%m%d%H%M%S")
                        except Exception:
                            pass
                    return True, snap_url, dt
        except Exception as e:
            logger.debug(f"Availability check error for {url}: {e}")
        return False, "", None

    def archive_url(
        self,
        url: str,
        check_existing: bool = True,
        skip_existing_days: int = 30,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> ArchiveResult:
        """Submit URL to Save Page Now and poll until completion."""
        def notify(msg: str):
            if status_callback:
                status_callback(msg)

        # 1. Check existing snapshot if requested
        if check_existing and skip_existing_days > 0:
            notify("Checking existing Wayback snapshot...")
            exists, snap_url, dt = self.check_existing_snapshot(url)
            if exists and snap_url:
                if dt:
                    now = datetime.datetime.utcnow()
                    age_days = (now - dt).days
                    if age_days <= skip_existing_days:
                        notify(f"Existing recent snapshot found ({age_days} days old). Skipping.")
                        return ArchiveResult(
                            original_url=url,
                            status="SKIPPED",
                            archive_url=snap_url,
                            capture_timestamp=dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
                            error=f"Existing snapshot is only {age_days} days old (skip threshold: {skip_existing_days}d)",
                            existing=True,
                        )
                elif skip_existing_days >= 365:
                    # If date parsing failed but skip enabled
                    return ArchiveResult(
                        original_url=url,
                        status="SKIPPED",
                        archive_url=snap_url,
                        capture_timestamp="",
                        error="Existing snapshot found",
                        existing=True,
                    )

        # 2. Submit capture to Save Page Now
        notify("Submitting capture request to Wayback Save Page Now...")
        for attempt in range(1, self.retries + 1):
            try:
                payload = {
                    "url": url,
                    "capture_all": "1",
                    "skip_first_archive": "1",
                    "if_not_archived_within": f"{skip_existing_days * 86400}" if skip_existing_days > 0 else "0",
                }
                
                resp = self.session.post(
                    SAVE_API_URL,
                    data=payload,
                    timeout=self.request_timeout,
                )

                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 30))
                    notify(f"Rate limited (HTTP 429). Waiting {retry_after}s...")
                    time.sleep(retry_after)
                    continue

                if resp.status_code in (401, 403):
                    return ArchiveResult(
                        original_url=url,
                        status="FAILED",
                        error=f"Authentication rejected (HTTP {resp.status_code}). Check Access & Secret keys.",
                        http_status=resp.status_code,
                    )

                try:
                    res_json = resp.json()
                except Exception:
                    res_json = {}

                # Check if immediate or job_id returned
                job_id = res_json.get("job_id")
                
                # Check for direct error message in JSON
                if "message" in res_json and not job_id:
                    err_msg = res_json.get("message", "")
                    # Sometimes message says URL already captured or cannot crawl
                    if "already" in err_msg.lower() or "recent" in err_msg.lower():
                        # Try availability lookup
                        exists, snap_url, dt = self.check_existing_snapshot(url)
                        if exists and snap_url:
                            return ArchiveResult(
                                original_url=url,
                                status="SUCCESS",
                                archive_url=snap_url,
                                capture_timestamp=dt.strftime("%Y-%m-%d %H:%M:%S UTC") if dt else "",
                                error=err_msg,
                                existing=True,
                            )
                    return ArchiveResult(
                        original_url=url,
                        status="FAILED",
                        error=err_msg or f"HTTP {resp.status_code}",
                        http_status=resp.status_code,
                    )

                if not job_id:
                    # If response redirected to archived URL or contains it in headers
                    location = resp.headers.get("Content-Location") or resp.headers.get("Location")
                    if location:
                        archive_url = f"https://web.archive.org{location}" if location.startswith("/") else location
                        return ArchiveResult(
                            original_url=url,
                            status="SUCCESS",
                            archive_url=archive_url,
                            capture_timestamp=datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
                            http_status=resp.status_code,
                        )

                    # Check availability fallback
                    time.sleep(3)
                    exists, snap_url, dt = self.check_existing_snapshot(url)
                    if exists:
                        return ArchiveResult(
                            original_url=url,
                            status="SUCCESS",
                            archive_url=snap_url,
                            capture_timestamp=dt.strftime("%Y-%m-%d %H:%M:%S UTC") if dt else "",
                            http_status=resp.status_code,
                        )
                    
                    return ArchiveResult(
                        original_url=url,
                        status="FAILED",
                        error=f"No job_id returned by Save Page Now (HTTP {resp.status_code})",
                        http_status=resp.status_code,
                    )

                # 3. Poll job status
                notify(f"Job queued ({job_id}). Waiting for capture to complete...")
                poll_result = self._poll_job(job_id, url, notify)
                if poll_result.status == "SUCCESS":
                    return poll_result
                elif attempt < self.retries and "timeout" in poll_result.error.lower():
                    notify(f"Job timed out, retrying (attempt {attempt + 1}/{self.retries})...")
                    time.sleep(5)
                    continue
                else:
                    return poll_result

            except requests.exceptions.RequestException as e:
                logger.warning(f"Request error for {url} (attempt {attempt}): {e}")
                if attempt < self.retries:
                    backoff = attempt * 5
                    notify(f"Network error: {e}. Retrying in {backoff}s...")
                    time.sleep(backoff)
                else:
                    return ArchiveResult(
                        original_url=url,
                        status="FAILED",
                        error=f"Network error: {e}",
                    )

        return ArchiveResult(
            original_url=url,
            status="FAILED",
            error=f"Exhausted all {self.retries} retries",
        )

    def _poll_job(
        self,
        job_id: str,
        original_url: str,
        notify: Callable[[str], None],
        max_poll_seconds: int = 150,
    ) -> ArchiveResult:
        """Poll Save Page Now status endpoint until completed or timeout."""
        start_time = time.time()
        status_url = f"{STATUS_API_URL}{job_id}"
        poll_interval = 4

        while time.time() - start_time < max_poll_seconds:
            time.sleep(poll_interval)
            try:
                resp = self.session.get(status_url, timeout=20)
                if resp.status_code == 200:
                    data = resp.json()
                    status = data.get("status")

                    if status == "success":
                        ts = data.get("timestamp")
                        orig = data.get("original_url", original_url)
                        if ts:
                            archive_url = f"https://web.archive.org/web/{ts}/{orig}"
                            formatted_ts = ts
                            try:
                                dt = datetime.datetime.strptime(ts[:14], "%Y%m%d%H%M%S")
                                formatted_ts = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
                            except Exception:
                                pass
                        else:
                            archive_url = f"https://web.archive.org/web/{orig}"
                            formatted_ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

                        notify("Capture completed successfully!")
                        return ArchiveResult(
                            original_url=original_url,
                            status="SUCCESS",
                            archive_url=archive_url,
                            capture_timestamp=formatted_ts,
                            http_status=data.get("http_status", 200),
                        )

                    elif status == "error":
                        err_msg = data.get("message") or data.get("status_ext", "Capture error")
                        notify(f"Capture failed: {err_msg}")
                        return ArchiveResult(
                            original_url=original_url,
                            status="FAILED",
                            error=err_msg,
                            http_status=data.get("http_status"),
                        )

                    elif status in ("pending", "in_progress"):
                        elapsed = int(time.time() - start_time)
                        notify(f"Capturing... ({status}, {elapsed}s elapsed)")
                    else:
                        notify(f"Status: {status}")

                elif resp.status_code == 429:
                    notify("Rate limited while polling status. Waiting 10s...")
                    time.sleep(10)

            except Exception as e:
                logger.debug(f"Poll check exception: {e}")

        # Check availability API one final time before giving up
        exists, snap_url, dt = self.check_existing_snapshot(original_url)
        if exists and snap_url:
            return ArchiveResult(
                original_url=original_url,
                status="SUCCESS",
                archive_url=snap_url,
                capture_timestamp=dt.strftime("%Y-%m-%d %H:%M:%S UTC") if dt else "",
                error="Completed (verified via availability check)",
            )

        return ArchiveResult(
            original_url=original_url,
            status="FAILED",
            error=f"Capture timed out after {max_poll_seconds}s waiting for job {job_id}",
        )
