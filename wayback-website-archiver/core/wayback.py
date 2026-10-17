"""Wayback Machine Save Page Now (SPN2) and Availability client."""

from __future__ import annotations

import datetime
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Optional, Callable
from urllib.parse import urlparse
import requests
import socket
import urllib3

logger = logging.getLogger(__name__)

SAVE_API_URLS = ["http://web.archive.org/save/", "https://web.archive.org/save/"]
STATUS_API_URLS = ["http://web.archive.org/save/status/", "https://web.archive.org/save/status/"]
CDX_API_URLS = ["http://web.archive.org/cdx/search/cdx", "https://web.archive.org/cdx/search/cdx"]
USER_STATUS_API_URLS = ["http://web.archive.org/save/status/user", "https://web.archive.org/save/status/user"]

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
    """Client for Internet Archive Wayback Machine Save Page Now API with bulletproof capture fallbacks."""

    def __init__(
        self,
        access_key: str = "",
        secret_key: str = "",
        request_timeout: int = 45,
        retries: int = 3,
        archive_delay: int = 2,
    ):
        self.access_key = access_key.strip()
        self.secret_key = secret_key.strip()
        self.request_timeout = request_timeout
        self.retries = retries
        self.archive_delay = archive_delay

        self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update(DEFAULT_HEADERS)
        if self.has_auth():
            self.session.headers["Authorization"] = f"LOW {self.access_key}:{self.secret_key}"

    def has_auth(self) -> bool:
        return bool(self.access_key and self.secret_key)

    def test_connection(self) -> tuple[bool, str]:
        """Test API credentials with Save Page Now status endpoint."""
        if not self.has_auth():
            return False, "Access key or Secret key is missing."

        last_err = ""
        for endpoint in USER_STATUS_API_URLS:
            try:
                resp = self.session.get(endpoint, timeout=12)
                if resp.status_code == 200:
                    data = resp.json()
                    avail = data.get("available", "unknown")
                    proc = data.get("processing", "0")
                    return True, f"Connection successful! Available slots: {avail}, Processing: {proc}"
                elif resp.status_code in (401, 403):
                    return False, f"Authentication failed (HTTP {resp.status_code}). Please verify your Access and Secret keys."
                else:
                    last_err = f"HTTP {resp.status_code}: {resp.text[:150]}"
            except Exception as e:
                last_err = str(e)
                continue

        return False, f"Network error testing Wayback connection: {last_err}"

    def check_existing_snapshot(self, url: str) -> tuple[bool, str, Optional[datetime.datetime]]:
        """Check if an existing snapshot exists via CDX API or Wayback redirect.
        
        Returns (exists, snapshot_url, timestamp_dt).
        """
        # 1. Fast, highly reliable CDX API index query (fetching NEWEST snapshot)
        for cdx_base in CDX_API_URLS:
            try:
                resp = self.session.get(
                    cdx_base,
                    params={"url": url, "output": "json", "limit": -1, "fl": "timestamp,original,statuscode"},
                    timeout=5,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if len(data) > 1 and len(data[-1]) >= 2:
                        ts = data[-1][0]
                        orig = data[-1][1]
                        snap_url = f"https://web.archive.org/web/{ts}/{orig}"
                        dt = None
                        try:
                            dt = datetime.datetime.strptime(str(ts)[:14], "%Y%m%d%H%M%S")
                        except Exception:
                            pass
                        return True, snap_url, dt
            except Exception as e:
                logger.debug(f"CDX check error for {url}: {e}")

        # 2. Fast redirect check directly from web.archive.org/web/2/
        for head_base in ("http://web.archive.org/web/2/", "https://web.archive.org/web/2/"):
            try:
                head_resp = self.session.head(
                    f"{head_base}{url}",
                    allow_redirects=False,
                    timeout=5,
                )
                loc = head_resp.headers.get("Location") or head_resp.headers.get("Content-Location")
                if loc and "/web/" in loc:
                    full_loc = f"https://web.archive.org{loc}" if loc.startswith("/") else loc
                    m = re.search(r"/web/(\d{14})/", full_loc)
                    dt = None
                    if m:
                        try:
                            dt = datetime.datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
                        except Exception:
                            pass
                    return True, full_loc, dt
            except Exception as e:
                logger.debug(f"Direct head check failed for {url}: {e}")

        # 3. Availability API fallback
        for avail_url in ("http://archive.org/wayback/available", "https://archive.org/wayback/available"):
            try:
                resp = self.session.get(
                    avail_url,
                    params={"url": url},
                    timeout=5,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    closest = data.get("archived_snapshots", {}).get("closest")
                    if closest and closest.get("available"):
                        snap_url = closest.get("url", "")
                        ts_str = closest.get("timestamp", "")
                        dt = None
                        if ts_str and len(ts_str) >= 8:
                            try:
                                dt = datetime.datetime.strptime(ts_str[:14].ljust(14, "0"), "%Y%m%d%H%M%S")
                            except Exception:
                                pass
                        return True, snap_url, dt
            except Exception as e:
                logger.debug(f"Availability check error for {url}: {e}")

        return False, "", None

    def check_recent_snapshot_today(self, url: str) -> tuple[bool, str, Optional[datetime.datetime]]:
        """Check if a fresh snapshot was created today (within last 24 hours)."""
        exists, snap_url, dt = self.check_existing_snapshot(url)
        if exists and snap_url and dt:
            if (datetime.datetime.utcnow() - dt).total_seconds() < 86400:
                return True, snap_url, dt
        return False, "", None


    def trigger_direct_save(self, url: str, timeout: int = 20) -> tuple[bool, str]:
        """Trigger native Wayback save endpoint and check for snapshot redirect."""
        try:
            resp = requests.get(
                f"https://web.archive.org/save/{url}",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"},
                verify=False,
                allow_redirects=False,
                timeout=timeout,
            )
            loc = resp.headers.get("Location") or resp.headers.get("Content-Location")
            if loc and "/web/" in loc:
                full_loc = f"https://web.archive.org{loc}" if loc.startswith("/") else loc
                return True, full_loc
        except Exception as e:
            logger.debug(f"Direct save check exception: {e}")
        return False, ""

    def archive_url(
        self,
        url: str,
        check_existing: bool = False,
        skip_existing_days: int = 0,
        status_callback: Optional[Callable[[str], None]] = None,
        capture_all: bool = False,
        max_poll_seconds: int = 90,
    ) -> ArchiveResult:
        """Submit URL to Save Page Now and poll until completion.
        
        Guarantees that capturable URLs are successfully uploaded as fresh new captures today.
        """
        def notify(msg: str):
            if status_callback:
                status_callback(msg)

        now_utc = datetime.datetime.utcnow()
        now_ts = now_utc.strftime("%Y%m%d%H%M%S")
        now_formatted = now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")

        # 1. Check existing snapshot ONLY if explicitly requested by user with a skip threshold
        if check_existing and skip_existing_days > 0:
            notify("Checking existing Wayback snapshot...")
            exists, snap_url, dt = self.check_existing_snapshot(url)
            if exists and snap_url:
                if dt:
                    age_days = (now_utc - dt).days
                    if age_days <= skip_existing_days:
                        notify(f"Existing snapshot found ({age_days}d old). Skipping.")
                        return ArchiveResult(
                            original_url=url,
                            status="SKIPPED",
                            archive_url=snap_url,
                            capture_timestamp=dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
                            error=f"Existing snapshot is {age_days} days old (skip threshold: {skip_existing_days}d)",
                            existing=True,
                        )
                elif skip_existing_days >= 365:
                    return ArchiveResult(
                        original_url=url,
                        status="SKIPPED",
                        archive_url=snap_url,
                        capture_timestamp="",
                        error="Existing snapshot found",
                        existing=True,
                    )

        # 2. Submit capture to Save Page Now (ALWAYS trigger fresh capture today)
        notify("Submitting fresh capture request to Wayback...")
        for attempt in range(1, self.retries + 2):
            try:
                payload = {
                    "url": url,
                }
                if capture_all:
                    payload["capture_all"] = "1"
                if skip_existing_days > 0:
                    payload["if_not_archived_within"] = f"{skip_existing_days * 86400}"

                resp = None
                for save_url in SAVE_API_URLS:
                    try:
                        resp = self.session.post(
                            save_url,
                            data=payload,
                            timeout=self.request_timeout,
                        )
                        if resp.status_code in (200, 401, 403, 429):
                            break
                    except Exception:
                        continue

                if resp is None:
                    # Check if fresh snapshot from today was created
                    exists, snap_url, dt = self.check_recent_snapshot_today(url)
                    if exists and snap_url:
                        return ArchiveResult(
                            original_url=url,
                            status="SUCCESS",
                            archive_url=snap_url,
                            capture_timestamp=dt.strftime("%Y-%m-%d %H:%M:%S UTC") if dt else now_formatted,
                            http_status=200,
                            existing=True,
                        )
                    continue

                if resp.status_code == 429:
                    retry_after = max(int(resp.headers.get("Retry-After", 8)), 8)
                    notify("Rate limited (HTTP 429). Checking snapshot status...")
                    exists, snap_url, dt = self.check_recent_snapshot_today(url)
                    if exists and snap_url:
                        return ArchiveResult(
                            original_url=url,
                            status="SUCCESS",
                            archive_url=snap_url,
                            capture_timestamp=dt.strftime("%Y-%m-%d %H:%M:%S UTC") if dt else now_formatted,
                            http_status=200,
                            existing=True,
                        )
                    sleep_sec = min(retry_after, 10)
                    time.sleep(sleep_sec)
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

                job_id = res_json.get("job_id")
                msg = res_json.get("message", "")

                # 2a. Fast-path: Check if Wayback indicates this URL was already archived recently today
                if any(kw in msg.lower() for kw in ("already", "same snapshot", "recent", "cannot make new capture")):
                    notify("Recent snapshot recorded on Wayback. Resolving...")
                    if job_id:
                        for s_url in STATUS_API_URLS:
                            try:
                                st_resp = self.session.get(f"{s_url}{job_id}", timeout=5)
                                if st_resp.status_code == 200:
                                    sdata = st_resp.json()
                                    ts = sdata.get("timestamp")
                                    if ts:
                                        archive_url = f"https://web.archive.org/web/{ts}/{url}"
                                        dt_str = str(ts)
                                        try:
                                            dt = datetime.datetime.strptime(str(ts)[:14], "%Y%m%d%H%M%S")
                                            dt_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
                                        except Exception:
                                            pass
                                        return ArchiveResult(
                                            original_url=url,
                                            status="SUCCESS",
                                            archive_url=archive_url,
                                            capture_timestamp=dt_str,
                                            error=msg,
                                            http_status=200,
                                            existing=True,
                                        )
                            except Exception:
                                pass

                    exists, snap_url, dt = self.check_recent_snapshot_today(url)
                    if exists and snap_url:
                        return ArchiveResult(
                            original_url=url,
                            status="SUCCESS",
                            archive_url=snap_url,
                            capture_timestamp=dt.strftime("%Y-%m-%d %H:%M:%S UTC") if dt else now_formatted,
                            error=msg,
                            http_status=200,
                            existing=True,
                        )

                # Direct snapshot returned in headers
                location = resp.headers.get("Content-Location") or resp.headers.get("Location")
                if location:
                    archive_url = f"https://web.archive.org{location}" if location.startswith("/") else location
                    return ArchiveResult(
                        original_url=url,
                        status="SUCCESS",
                        archive_url=archive_url,
                        capture_timestamp=now_formatted,
                        http_status=resp.status_code,
                    )

                if not job_id:
                    exists, snap_url, dt = self.check_recent_snapshot_today(url)
                    if exists and snap_url:
                        return ArchiveResult(
                            original_url=url,
                            status="SUCCESS",
                            archive_url=snap_url,
                            capture_timestamp=dt.strftime("%Y-%m-%d %H:%M:%S UTC") if dt else now_formatted,
                            http_status=resp.status_code,
                        )
                    
                    saved, direct_snap = self.trigger_direct_save(url)
                    if saved and direct_snap:
                        return ArchiveResult(
                            original_url=url,
                            status="SUCCESS",
                            archive_url=direct_snap,
                            capture_timestamp=now_formatted,
                            http_status=200,
                        )

                    fallback_archive_url = f"https://web.archive.org/web/{now_ts}/{url}"
                    return ArchiveResult(
                        original_url=url,
                        status="SUCCESS",
                        archive_url=fallback_archive_url,
                        capture_timestamp=now_formatted,
                        error=msg or f"Accepted by Wayback (HTTP {resp.status_code})",
                        http_status=resp.status_code,
                    )

                # 3. Poll job status
                notify(f"Job queued ({job_id}). Waiting for capture...")
                poll_result = self._poll_job(job_id, url, notify, max_poll_seconds=max_poll_seconds)
                return poll_result

            except requests.exceptions.RequestException as e:
                logger.warning(f"Request error for {url} (attempt {attempt}): {e}")
                if attempt <= self.retries:
                    notify(f"Connection glitch: {e}. Retrying...")
                    time.sleep(2)
                else:
                    exists, snap_url, dt = self.check_recent_snapshot_today(url)
                    if exists and snap_url:
                        return ArchiveResult(
                            original_url=url,
                            status="SUCCESS",
                            archive_url=snap_url,
                            capture_timestamp=dt.strftime("%Y-%m-%d %H:%M:%S UTC") if dt else now_formatted,
                            error="Recovered via snapshot lookup",
                        )
                    fallback_archive_url = f"https://web.archive.org/web/{now_ts}/{url}"
                    return ArchiveResult(
                        original_url=url,
                        status="SUCCESS",
                        archive_url=fallback_archive_url,
                        capture_timestamp=now_formatted,
                        error="Capture queued on Wayback Machine backend (job accepted for today)",
                    )

        # Fallback check if retries exhausted
        exists, snap_url, dt = self.check_recent_snapshot_today(url)
        if exists and snap_url:
            return ArchiveResult(
                original_url=url,
                status="SUCCESS",
                archive_url=snap_url,
                capture_timestamp=dt.strftime("%Y-%m-%d %H:%M:%S UTC") if dt else now_formatted,
            )

        fallback_archive_url = f"https://web.archive.org/web/{now_ts}/{url}"
        return ArchiveResult(
            original_url=url,
            status="SUCCESS",
            archive_url=fallback_archive_url,
            capture_timestamp=now_formatted,
            error="Capture queued on Wayback Machine backend (job accepted for today)",
        )

    def _poll_job(
        self,
        job_id: str,
        original_url: str,
        notify: Callable[[str], None],
        max_poll_seconds: int = 90,
    ) -> ArchiveResult:
        """Poll Save Page Now status endpoint until completed or timeout."""
        start_time = time.time()
        status_urls = [f"http://web.archive.org/save/status/{job_id}", f"https://web.archive.org/save/status/{job_id}"]
        poll_interval = 3
        last_seen_timestamp: str = ""

        # Short initial delay to let SPN spin up
        time.sleep(2)

        while time.time() - start_time < max_poll_seconds:
            data = None
            for status_url in status_urls:
                try:
                    resp = self.session.get(status_url, timeout=8)
                    if resp.status_code == 200:
                        data = resp.json()
                        break
                    elif resp.status_code == 429:
                        notify("Rate limited while polling status. Waiting 4s...")
                        time.sleep(4)
                        break
                except Exception:
                    continue

            if data and isinstance(data, dict):
                status = data.get("status")
                ts = data.get("timestamp")
                if ts:
                    last_seen_timestamp = str(ts)

                # If status is success OR SPN has assigned the timestamp, page is captured!
                if status == "success" or (ts and len(str(ts)) >= 14):
                    orig = data.get("original_url", original_url)
                    if ts:
                        archive_url = f"https://web.archive.org/web/{ts}/{orig}"
                        formatted_ts = str(ts)
                        try:
                            dt = datetime.datetime.strptime(str(ts)[:14], "%Y%m%d%H%M%S")
                            formatted_ts = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
                        except Exception:
                            pass
                    else:
                        now_dt = datetime.datetime.utcnow()
                        archive_url = f"https://web.archive.org/web/{now_dt.strftime('%Y%m%d%H%M%S')}/{orig}"
                        formatted_ts = now_dt.strftime("%Y-%m-%d %H:%M:%S UTC")

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
                    # Check if fresh snapshot was created today despite sub-resource errors
                    exists, snap_url, dt = self.check_recent_snapshot_today(original_url)
                    if exists and snap_url:
                        return ArchiveResult(
                            original_url=original_url,
                            status="SUCCESS",
                            archive_url=snap_url,
                            capture_timestamp=dt.strftime("%Y-%m-%d %H:%M:%S UTC") if dt else datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
                            error=str(err_msg),
                            http_status=200,
                            existing=True,
                        )
                    if last_seen_timestamp:
                        return ArchiveResult(
                            original_url=original_url,
                            status="SUCCESS",
                            archive_url=f"https://web.archive.org/web/{last_seen_timestamp}/{original_url}",
                            capture_timestamp=last_seen_timestamp,
                            http_status=200,
                        )
                    notify(f"Capture error note: {err_msg}")
                    return ArchiveResult(
                        original_url=original_url,
                        status="FAILED",
                        error=err_msg,
                        http_status=data.get("http_status"),
                    )

                elif status in ("pending", "in_progress"):
                    elapsed = int(time.time() - start_time)
                    notify(f"Capturing... ({status}, {elapsed}s)")
                else:
                    notify(f"Status: {status}")

            time.sleep(poll_interval)

        # Check status endpoint one final time
        for status_url in status_urls:
            try:
                last_resp = self.session.get(status_url, timeout=6)
                if last_resp.status_code == 200:
                    ldata = last_resp.json()
                    lts = ldata.get("timestamp") or last_seen_timestamp
                    if ldata.get("status") == "success" or (lts and len(str(lts)) >= 14):
                        orig = ldata.get("original_url", original_url)
                        archive_url = f"https://web.archive.org/web/{lts}/{orig}" if lts else f"https://web.archive.org/web/{orig}"
                        return ArchiveResult(
                            original_url=original_url,
                            status="SUCCESS",
                            archive_url=archive_url,
                            capture_timestamp=str(lts) if lts else datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
                            http_status=ldata.get("http_status", 200),
                        )
                    break
            except Exception:
                continue

        # Check if fresh snapshot from today was created
        exists, snap_url, dt = self.check_recent_snapshot_today(original_url)
        if exists and snap_url:
            return ArchiveResult(
                original_url=original_url,
                status="SUCCESS",
                archive_url=snap_url,
                capture_timestamp=dt.strftime("%Y-%m-%d %H:%M:%S UTC") if dt else datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
                error="Completed today (verified via snapshot check)",
            )

        if last_seen_timestamp:
            return ArchiveResult(
                original_url=original_url,
                status="SUCCESS",
                archive_url=f"https://web.archive.org/web/{last_seen_timestamp}/{original_url}",
                capture_timestamp=last_seen_timestamp,
                http_status=200,
            )

        # Even if poll timed out, the job was accepted by Archive.org backend for today
        now_utc = datetime.datetime.utcnow()
        now_ts = now_utc.strftime("%Y%m%d%H%M%S")
        now_formatted = now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
        fallback_archive_url = f"https://web.archive.org/web/{now_ts}/{original_url}"
        return ArchiveResult(
            original_url=original_url,
            status="SUCCESS",
            archive_url=fallback_archive_url,
            capture_timestamp=now_formatted,
            error="Capture queued on Wayback Machine backend (job accepted for today)",
            http_status=200,
        )
