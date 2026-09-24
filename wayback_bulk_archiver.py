#!/usr/bin/env python3
"""
Wayback Bulk Archiver v2.0.0
=============================
Automatically crawl a website to discover all pages, then bulk-submit
every URL to the Internet Archive Wayback Machine "Save Page Now" service.

Usage:
    # Auto-crawl a site and archive everything:
    python wayback_bulk_archiver.py https://example.com --crawl

    # Use a pre-made URL list:
    python wayback_bulk_archiver.py urls.txt

    # With authentication (REQUIRED by Internet Archive):
    python wayback_bulk_archiver.py https://example.com --crawl --access-key YOUR_KEY --secret-key YOUR_SECRET

Author:  Automated tooling
License: MIT
"""

from __future__ import annotations

__version__ = "2.0.0"

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from collections import deque
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse, urlunparse

# ---------------------------------------------------------------------------
# Third-party imports (documented in requirements.txt)
# ---------------------------------------------------------------------------
try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    print(
        "ERROR: The 'requests' library is required.\n"
        "Install it with:  pip install requests\n"
        "Or:               pip install -r requirements.txt",
        file=sys.stderr,
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SAVE_ENDPOINT = "https://web.archive.org/save"
SAVE_STATUS_ENDPOINT = "https://web.archive.org/save/status"
AVAILABILITY_ENDPOINT = "https://archive.org/wayback/available"

DEFAULT_DELAY = 15
DEFAULT_RETRIES = 3
DEFAULT_TIMEOUT = 60
DEFAULT_CRAWL_DEPTH = 3
DEFAULT_USER_AGENT = f"WaybackBulkArchiver/{__version__} (bulk archival tool)"
DEFAULT_OUTPUT_DIR = "."

ARCHIVE_URL_RE = re.compile(
    r"https?://web\.archive\.org/web/\d{14}[^\"'\s<>]*"
)

# File extensions to skip when crawling (not web pages)
SKIP_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".bmp",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".rar", ".gz", ".tar", ".7z",
    ".mp3", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".css", ".js", ".json", ".xml", ".rss", ".atom",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(log_file: str) -> logging.Logger:
    """Configure file + console logging and return the logger."""
    logger = logging.getLogger("wayback_archiver")
    logger.setLevel(logging.DEBUG)

    # Remove existing handlers (in case of re-run)
    logger.handlers.clear()

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(ch)

    return logger


# ---------------------------------------------------------------------------
# HTML Link Extractor (stdlib — no extra dependencies)
# ---------------------------------------------------------------------------

class LinkExtractor(HTMLParser):
    """Extract href attributes from <a> tags in HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value:
                    self.links.append(value)


def extract_links_from_html(html: str) -> list[str]:
    """Parse HTML and return all <a href> values."""
    parser = LinkExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    return parser.links


# ---------------------------------------------------------------------------
# Site Crawler
# ---------------------------------------------------------------------------

def crawl_site_urls(
    start_url: str,
    *,
    max_depth: int = DEFAULT_CRAWL_DEPTH,
    timeout: int = DEFAULT_TIMEOUT,
    user_agent: str = DEFAULT_USER_AGENT,
    delay: float = 1.0,
    logger: Optional[logging.Logger] = None,
) -> list[str]:
    """Crawl a website starting from start_url and discover all internal pages.

    Uses breadth-first search up to max_depth levels.
    Only follows links on the same domain.
    Skips non-page resources (images, PDFs, etc.).

    Returns a deduplicated list of discovered page URLs.
    """
    parsed_start = urlparse(start_url)
    base_domain = parsed_start.netloc.lower()
    base_scheme = parsed_start.scheme.lower()

    # Normalize the start URL
    if not base_domain:
        print(f"  ERROR: Invalid URL: {start_url}", file=sys.stderr)
        return []

    print(f"\n  Crawling {start_url} (depth={max_depth})...")
    print(f"  Domain: {base_domain}\n")

    visited: set[str] = set()
    discovered: list[str] = []
    queue: deque[tuple[str, int]] = deque()

    # Start with the given URL
    norm_start = _normalize_crawl_url(start_url)
    visited.add(norm_start)
    queue.append((start_url, 0))
    discovered.append(start_url)

    session = requests.Session()
    session.headers.update({
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml",
    })

    page_count = 0

    while queue:
        current_url, depth = queue.popleft()

        if depth > max_depth:
            continue

        # Fetch the page
        try:
            if page_count > 0:
                time.sleep(delay)

            resp = session.get(current_url, timeout=timeout, allow_redirects=True)
            page_count += 1

            content_type = resp.headers.get("Content-Type", "")
            if "text/html" not in content_type:
                continue

            if logger:
                logger.debug("Crawl [depth=%d] %s -> HTTP %d", depth, current_url, resp.status_code)

        except Exception as exc:
            if logger:
                logger.warning("Crawl error for %s: %s", current_url, exc)
            continue

        # Extract links
        links = extract_links_from_html(resp.text)

        for href in links:
            # Skip anchors, javascript, mailto, tel
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue

            # Resolve relative URLs
            full_url = urljoin(current_url, href)

            # Parse and validate
            parsed = urlparse(full_url)

            # Only same domain
            if parsed.netloc.lower() != base_domain:
                continue

            # Only http/https
            if parsed.scheme not in ("http", "https"):
                continue

            # Skip non-page extensions
            path_lower = parsed.path.lower()
            ext = os.path.splitext(path_lower)[1]
            if ext in SKIP_EXTENSIONS:
                continue

            # Skip WordPress admin, login, feed paths
            if any(skip in path_lower for skip in [
                "/wp-admin", "/wp-login", "/wp-content", "/wp-includes",
                "/feed", "/xmlrpc", "/wp-json", "/cart", "/checkout",
                "/my-account", "?add-to-cart",
            ]):
                continue

            # Remove fragment, keep path and query
            clean_url = urlunparse((
                parsed.scheme, parsed.netloc, parsed.path,
                parsed.params, parsed.query, "",
            ))

            norm = _normalize_crawl_url(clean_url)
            if norm not in visited:
                visited.add(norm)
                discovered.append(clean_url)
                print(f"  Found: {clean_url}")
                if logger:
                    logger.info("Discovered: %s", clean_url)

                # Continue crawling if within depth
                if depth + 1 <= max_depth:
                    queue.append((clean_url, depth + 1))

    session.close()

    print(f"\n  Crawl complete: {len(discovered)} pages found.\n")
    if logger:
        logger.info("Crawl complete: %d pages discovered from %s", len(discovered), start_url)

    return discovered


def _normalize_crawl_url(url: str) -> str:
    """Normalize URL for crawl deduplication."""
    parsed = urlparse(url)
    # Lowercase scheme and host, strip trailing slash from path for comparison
    path = parsed.path.rstrip("/") if parsed.path != "/" else "/"
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}".lower()


# ---------------------------------------------------------------------------
# URL loading & validation
# ---------------------------------------------------------------------------

def load_urls(source: str) -> list[str]:
    """Load URLs from a text file, one per line.

    Ignores blank lines and lines starting with ``#``.
    """
    urls: list[str] = []
    path = Path(source)
    if not path.is_file():
        raise FileNotFoundError(f"URL file not found: {source}")

    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            urls.append(line)
    return urls


def validate_url(url: str) -> tuple[bool, str]:
    """Return (is_valid, message) for a single URL."""
    try:
        parsed = urlparse(url)
    except Exception as exc:
        return False, f"Malformed URL: {exc}"

    if parsed.scheme not in ("http", "https"):
        return False, f"Unsupported scheme '{parsed.scheme}' (only http/https)"
    if not parsed.netloc:
        return False, "Missing hostname"
    return True, "OK"


def normalize_url(url: str) -> str:
    """Return a lightly normalised URL for deduplication."""
    parsed = urlparse(url)
    normalized = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
    )
    return normalized.geturl()


def deduplicate_urls(urls: list[str]) -> list[str]:
    """Remove duplicates while preserving order."""
    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        key = normalize_url(url)
        if key not in seen:
            seen.add(key)
            unique.append(url)
    return unique


# ---------------------------------------------------------------------------
# HTTP session helpers
# ---------------------------------------------------------------------------

def build_session(
    user_agent: str,
    retries: int = 0,
    access_key: str = "",
    secret_key: str = "",
) -> requests.Session:
    """Build a requests.Session with retry adapter, custom UA, and optional auth."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": user_agent,
        "Accept": "application/json",
    })

    # Internet Archive S3-style authentication
    if access_key and secret_key:
        session.headers["Authorization"] = f"LOW {access_key}:{secret_key}"

    if retries > 0:
        retry_strategy = Retry(
            total=retries,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503],
            allowed_methods=["GET", "POST"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

    return session


# ---------------------------------------------------------------------------
# Wayback Machine interaction
# ---------------------------------------------------------------------------

def check_existing_snapshot(
    session: requests.Session,
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    logger: Optional[logging.Logger] = None,
) -> Optional[dict[str, Any]]:
    """Query the Wayback Availability API for an existing snapshot."""
    try:
        resp = session.get(
            AVAILABILITY_ENDPOINT,
            params={"url": url},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        closest = data.get("archived_snapshots", {}).get("closest")
        if closest and closest.get("available"):
            return {
                "url": closest.get("url", ""),
                "timestamp": closest.get("timestamp", ""),
                "status": closest.get("status", ""),
            }
    except Exception as exc:
        if logger:
            logger.warning("Availability check failed for %s: %s", url, exc)
    return None


def submit_to_wayback(
    session: requests.Session,
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    logger: Optional[logging.Logger] = None,
) -> dict[str, Any]:
    """Submit a URL to the Save Page Now service.

    Returns a result dict with keys:
        status, archive_url, http_status, message, job_id
    """
    result: dict[str, Any] = {
        "status": "failed",
        "archive_url": "",
        "http_status": 0,
        "message": "",
        "job_id": "",
    }

    try:
        resp = session.post(
            SAVE_ENDPOINT,
            data={"url": url, "capture_all": "on"},
            timeout=timeout,
            allow_redirects=True,
        )
        result["http_status"] = resp.status_code

        if logger:
            logger.debug(
                "SPN response for %s: HTTP %d, headers=%s",
                url, resp.status_code, dict(resp.headers),
            )

        # ---- Parse the response to find the archive URL ----
        archive_url = _extract_archive_url(resp, url, logger)

        if archive_url:
            result["status"] = "success"
            result["archive_url"] = archive_url
            result["message"] = "Captured successfully"
        else:
            # Check for SPN2 job_id -> poll for completion
            job_id = _extract_job_id(resp)
            if job_id:
                result["job_id"] = job_id
                if logger:
                    logger.info("Got job_id=%s for %s, polling...", job_id, url)
                poll_result = _poll_job_status(session, job_id, timeout, logger)
                if poll_result:
                    result["status"] = "success"
                    result["archive_url"] = poll_result
                    result["message"] = "Captured successfully (via job poll)"
                else:
                    result["message"] = (
                        f"Job {job_id} submitted but could not confirm capture"
                    )
            else:
                # Fallback: try GET to /save/{url} (older SPN1 style)
                archive_url = _try_get_save(session, url, timeout, logger)
                if archive_url:
                    result["status"] = "success"
                    result["archive_url"] = archive_url
                    result["message"] = "Captured successfully (via GET fallback)"
                else:
                    result["message"] = _extract_error_message(resp)

    except requests.exceptions.Timeout:
        result["message"] = "Request timed out"
        if logger:
            logger.error("Timeout submitting %s", url)
    except requests.exceptions.ConnectionError as exc:
        result["message"] = f"Connection error: {exc}"
        if logger:
            logger.error("Connection error for %s: %s", url, exc)
    except requests.exceptions.RequestException as exc:
        result["message"] = f"Request error: {exc}"
        if logger:
            logger.error("Request error for %s: %s", url, exc)

    return result


def _extract_archive_url(
    resp: requests.Response,
    original_url: str,
    logger: Optional[logging.Logger] = None,
) -> str:
    """Try to extract an archive URL from the response."""
    # 1. Content-Location header
    content_loc = resp.headers.get("Content-Location", "")
    if content_loc and "/web/" in content_loc:
        if content_loc.startswith("/"):
            content_loc = "https://web.archive.org" + content_loc
        return content_loc

    # 2. Final redirect URL
    if resp.url and "/web/" in resp.url and "web.archive.org" in resp.url:
        return resp.url

    # 3. Link header
    link_header = resp.headers.get("Link", "")
    if link_header:
        match = ARCHIVE_URL_RE.search(link_header)
        if match:
            return match.group(0)

    # 4. JSON body
    try:
        data = resp.json()
        for key in ("url", "archive_url", "wayback_id"):
            val = data.get(key, "")
            if val and "web.archive.org" in val:
                return val
        resources = data.get("resources", [])
        if resources and isinstance(resources, list):
            for r in resources:
                if isinstance(r, str) and "web.archive.org" in r:
                    return r
    except (ValueError, AttributeError):
        pass

    # 5. Regex scan of body
    body = resp.text or ""
    match = ARCHIVE_URL_RE.search(body)
    if match:
        return match.group(0)

    return ""


def _extract_job_id(resp: requests.Response) -> str:
    """Extract a SPN2 job_id from a JSON response."""
    try:
        data = resp.json()
        return data.get("job_id", "")
    except (ValueError, AttributeError):
        return ""


def _poll_job_status(
    session: requests.Session,
    job_id: str,
    timeout: int = DEFAULT_TIMEOUT,
    logger: Optional[logging.Logger] = None,
    max_polls: int = 30,
    poll_interval: int = 6,
) -> str:
    """Poll SPN2 /save/status/{job_id} until completion or timeout."""
    status_url = f"{SAVE_STATUS_ENDPOINT}/{job_id}"
    for attempt in range(1, max_polls + 1):
        try:
            resp = session.get(status_url, timeout=timeout)
            data = resp.json()
            status = data.get("status", "")
            if logger:
                logger.debug("Job %s poll #%d: status=%s", job_id, attempt, status)
            if status == "success":
                ts = data.get("timestamp", "")
                orig = data.get("original_url", "")
                if ts and orig:
                    return f"https://web.archive.org/web/{ts}/{orig}"
                for key in ("archive_url", "url"):
                    val = data.get(key, "")
                    if val and "web.archive.org" in val:
                        return val
                return ""
            if status == "error":
                msg = data.get("message", "unknown error")
                if logger:
                    logger.warning("Job %s failed: %s", job_id, msg)
                return ""
        except Exception as exc:
            if logger:
                logger.warning("Poll error for job %s: %s", job_id, exc)
        time.sleep(poll_interval)

    if logger:
        logger.warning("Job %s: polling timed out after %d attempts", job_id, max_polls)
    return ""


def _try_get_save(
    session: requests.Session,
    url: str,
    timeout: int,
    logger: Optional[logging.Logger] = None,
) -> str:
    """Fallback: issue a GET to /save/{url} (SPN1 style)."""
    try:
        resp = session.get(
            f"{SAVE_ENDPOINT}/{url}",
            timeout=timeout,
            allow_redirects=True,
        )
        return _extract_archive_url(resp, url, logger)
    except Exception:
        pass
    return ""


def _extract_error_message(resp: requests.Response) -> str:
    """Best-effort extraction of an error message."""
    try:
        data = resp.json()
        msg = data.get("message", "") or data.get("error", "")
        if msg:
            return str(msg)
    except (ValueError, AttributeError):
        pass
    if resp.status_code == 429:
        return "Rate limited (HTTP 429). Increase --delay."
    if resp.status_code == 403:
        return "Forbidden (HTTP 403). Authentication required -- use --access-key and --secret-key."
    if resp.status_code >= 500:
        return f"Server error (HTTP {resp.status_code})"
    return f"Unexpected response (HTTP {resp.status_code})"


def parse_archive_response(result: dict[str, Any]) -> dict[str, Any]:
    """Normalise and enrich a raw result dict for output."""
    return result


# ---------------------------------------------------------------------------
# Result persistence
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "original_url", "status", "archive_url",
    "timestamp", "http_status", "message",
]


def save_result(
    result: dict[str, Any],
    csv_path: str,
    json_path: str,
    all_results: list[dict[str, Any]],
) -> None:
    """Persist a single result: append to CSV and rewrite JSON."""
    all_results.append(result)
    _append_csv(result, csv_path)
    _write_json(all_results, json_path)


def _append_csv(result: dict[str, Any], csv_path: str) -> None:
    """Append one result row to the CSV file."""
    file_exists = os.path.isfile(csv_path) and os.path.getsize(csv_path) > 0
    with open(csv_path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerow(result)


def _write_json(results: list[dict[str, Any]], json_path: str) -> None:
    """Write the full results list to JSON."""
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, ensure_ascii=False)


def write_csv(results: list[dict[str, Any]], csv_path: str) -> None:
    """Write all results to CSV from scratch."""
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in results:
            writer.writerow(row)


def write_json(results: list[dict[str, Any]], json_path: str) -> None:
    """Write all results to JSON from scratch."""
    _write_json(results, json_path)


# ---------------------------------------------------------------------------
# Resume support
# ---------------------------------------------------------------------------

def load_completed_urls(csv_path: str) -> set[str]:
    """Load URLs that were successfully archived from a previous run."""
    completed: set[str] = set()
    if not os.path.isfile(csv_path):
        return completed
    try:
        with open(csv_path, "r", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                status = (row.get("status") or "").strip().lower()
                archive_url = (row.get("archive_url") or "").strip()
                original = (row.get("original_url") or "").strip()
                if status in ("success", "already_exists") and archive_url:
                    completed.add(normalize_url(original))
    except Exception:
        pass
    return completed


def load_previous_results(csv_path: str) -> list[dict[str, Any]]:
    """Reload all previous results from CSV."""
    results: list[dict[str, Any]] = []
    if not os.path.isfile(csv_path):
        return results
    try:
        with open(csv_path, "r", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                results.append(dict(row))
    except Exception:
        pass
    return results


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

def run_batch(
    urls: list[str],
    *,
    delay: int,
    retries: int,
    timeout: int,
    user_agent: str,
    output_dir: str,
    dry_run: bool,
    resume: bool,
    skip_existing: bool,
    max_urls: Optional[int],
    access_key: str,
    secret_key: str,
    logger: logging.Logger,
) -> None:
    """Execute the full batch-submission workflow."""

    csv_path = os.path.join(output_dir, "results.csv")
    json_path = os.path.join(output_dir, "results.json")

    # --resume: load previously completed URLs
    completed: set[str] = set()
    all_results: list[dict[str, Any]] = []
    if resume:
        completed = load_completed_urls(csv_path)
        all_results = load_previous_results(csv_path)
        if completed:
            logger.info("Resume: %d URLs already completed.", len(completed))
            print(f"\n  Resuming -- {len(completed)} URLs already completed.\n")

    # --max-urls cap
    if max_urls is not None and max_urls > 0:
        urls = urls[:max_urls]

    total = len(urls)
    success_count = 0
    fail_count = 0
    skip_count = 0

    auth_status = "YES (API keys)" if (access_key and secret_key) else "NO (anonymous)"

    # Header
    print("\n" + "=" * 56)
    print("  WAYBACK BULK ARCHIVER  v" + __version__)
    print("=" * 56)
    print(f"\n  Total URLs : {total}")
    print(f"  Delay      : {delay}s")
    print(f"  Retries    : {retries}")
    print(f"  Timeout    : {timeout}s")
    print(f"  Auth       : {auth_status}")
    print(f"  Output     : {output_dir}")
    if dry_run:
        print("  Mode       : DRY RUN (no submissions)")
    if resume:
        print(f"  Resume     : ON ({len(completed)} already done)")
    if skip_existing:
        print("  Skip existing: ON")

    if not access_key or not secret_key:
        print("\n  WARNING: No API keys provided!")
        print("  Internet Archive REQUIRES authentication for Save Page Now.")
        print("  Get your free keys at: https://archive.org/account/s3.php")
        print("  Then run with: --access-key YOUR_KEY --secret-key YOUR_SECRET")
        if not dry_run:
            print("\n  Continuing anyway, but submissions will likely fail...\n")

    print()

    session = build_session(user_agent, retries=retries, access_key=access_key, secret_key=secret_key)

    for idx, url in enumerate(urls, 1):
        now_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print("-" * 56)
        print(f"  [{idx}/{total}]  {url}")

        # --resume skip
        if resume and normalize_url(url) in completed:
            print("  >> SKIPPED (already archived)")
            skip_count += 1
            logger.info("[%d/%d] Skipped (resume): %s", idx, total, url)
            if idx < total:
                print()
            continue

        # --dry-run
        if dry_run:
            valid, msg = validate_url(url)
            status_label = "VALID" if valid else f"INVALID -- {msg}"
            print(f"  [?] {status_label}")
            logger.info("[%d/%d] Dry-run: %s -- %s", idx, total, url, status_label)
            if idx < total:
                print()
            continue

        # Validate
        valid, msg = validate_url(url)
        if not valid:
            result = {
                "original_url": url,
                "status": "failed",
                "archive_url": "",
                "timestamp": now_ts,
                "http_status": 0,
                "message": msg,
            }
            save_result(result, csv_path, json_path, all_results)
            fail_count += 1
            print(f"  [X] INVALID -- {msg}")
            logger.warning("[%d/%d] Invalid URL: %s -- %s", idx, total, url, msg)
            if idx < total:
                print()
            continue

        # --skip-existing check
        if skip_existing:
            print("  ... Checking for existing snapshot...")
            logger.info("[%d/%d] Checking existing snapshot: %s", idx, total, url)
            existing = check_existing_snapshot(session, url, timeout, logger)
            if existing and existing.get("url"):
                result = {
                    "original_url": url,
                    "status": "already_exists",
                    "archive_url": existing["url"],
                    "timestamp": now_ts,
                    "http_status": int(existing.get("status", 0)),
                    "message": f"Existing snapshot from {existing.get('timestamp', 'unknown')}",
                }
                save_result(result, csv_path, json_path, all_results)
                skip_count += 1
                completed.add(normalize_url(url))
                print(f"  >> ALREADY EXISTS")
                print(f"     {existing['url']}")
                logger.info("[%d/%d] Already exists: %s", idx, total, url)
                if idx < total:
                    _wait(delay, idx, total)
                continue

        # Submit
        print("  ... Submitting to Wayback Machine...")
        logger.info("[%d/%d] Submitting: %s", idx, total, url)

        raw_result = submit_to_wayback(session, url, timeout, logger)
        result_data = parse_archive_response(raw_result)

        result = {
            "original_url": url,
            "status": result_data["status"],
            "archive_url": result_data.get("archive_url", ""),
            "timestamp": now_ts,
            "http_status": result_data.get("http_status", 0),
            "message": result_data.get("message", ""),
        }

        save_result(result, csv_path, json_path, all_results)

        if result_data["status"] == "success":
            success_count += 1
            completed.add(normalize_url(url))
            print(f"  [OK] SUCCESS")
            if result_data.get("archive_url"):
                print(f"     {result_data['archive_url']}")
            logger.info("[%d/%d] Success: %s -> %s", idx, total, url, result_data.get("archive_url", ""))
        else:
            fail_count += 1
            print(f"  [X] FAILED -- {result_data.get('message', 'unknown')}")
            logger.warning("[%d/%d] Failed: %s -- %s", idx, total, url, result_data.get("message", ""))

        # Delay before next
        if idx < total:
            _wait(delay, idx, total)

    # ---- Final rewrite (clean) ----
    if not dry_run and all_results:
        write_csv(all_results, csv_path)
        write_json(all_results, json_path)

    # ---- Summary ----
    print()
    print("=" * 56)
    print("  COMPLETE")
    print("=" * 56)
    print(f"\n  Total      : {total}")
    if not dry_run:
        print(f"  Successful : {success_count}")
        print(f"  Failed     : {fail_count}")
        print(f"  Skipped    : {skip_count}")
        print(f"\n  Results:")
        print(f"    {csv_path}")
        print(f"    {json_path}")
    else:
        print("  (dry-run -- no submissions made)")
    log_path = os.path.join(output_dir, "wayback_archiver.log")
    print(f"    {log_path}")
    print()


def _wait(delay: int, idx: int, total: int) -> None:
    """Print a countdown and sleep."""
    if delay <= 0:
        return
    print(f"\n  Waiting {delay}s before next request...", end="", flush=True)
    time.sleep(delay)
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    parser = argparse.ArgumentParser(
        prog="wayback_bulk_archiver",
        description=(
            "Bulk-submit URLs to the Internet Archive Wayback Machine.\n\n"
            "AUTO-CRAWL MODE: Give a website URL with --crawl to auto-discover all pages.\n"
            "FILE MODE: Give a urls.txt file with one URL per line.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "EXAMPLES:\n"
            "  # Auto-crawl a site and archive everything:\n"
            "  python wayback_bulk_archiver.py https://example.com --crawl --access-key KEY --secret-key SECRET\n\n"
            "  # Use a URL list file:\n"
            "  python wayback_bulk_archiver.py urls.txt --access-key KEY --secret-key SECRET\n\n"
            "  # Dry run (no submissions):\n"
            "  python wayback_bulk_archiver.py https://example.com --crawl --dry-run\n\n"
            "  # Get your FREE API keys at: https://archive.org/account/s3.php\n"
        ),
    )

    parser.add_argument(
        "urls",
        nargs="+",
        help="A website URL to crawl (with --crawl), a urls.txt file, or individual URLs.",
    )

    # ---- Crawl options ----
    crawl_group = parser.add_argument_group("Crawl options")
    crawl_group.add_argument(
        "--crawl",
        action="store_true",
        help="Auto-crawl the website to discover all internal page links.",
    )
    crawl_group.add_argument(
        "--crawl-depth",
        type=int,
        default=DEFAULT_CRAWL_DEPTH,
        help=f"How many levels deep to crawl (default: {DEFAULT_CRAWL_DEPTH}).",
    )
    crawl_group.add_argument(
        "--save-urls",
        type=str,
        default=None,
        help="Save discovered URLs to a file (e.g. --save-urls discovered.txt).",
    )

    # ---- Authentication ----
    auth_group = parser.add_argument_group("Authentication (REQUIRED by Internet Archive)")
    auth_group.add_argument(
        "--access-key",
        type=str,
        default=os.environ.get("IA_ACCESS_KEY", ""),
        help="Internet Archive S3 Access Key. Or set env var IA_ACCESS_KEY.",
    )
    auth_group.add_argument(
        "--secret-key",
        type=str,
        default=os.environ.get("IA_SECRET_KEY", ""),
        help="Internet Archive S3 Secret Key. Or set env var IA_SECRET_KEY.",
    )

    # ---- Submission options ----
    submit_group = parser.add_argument_group("Submission options")
    submit_group.add_argument(
        "--delay",
        type=int,
        default=DEFAULT_DELAY,
        help=f"Seconds between submissions (default: {DEFAULT_DELAY}).",
    )
    submit_group.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"Max retries for failures (default: {DEFAULT_RETRIES}).",
    )
    submit_group.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"HTTP timeout in seconds (default: {DEFAULT_TIMEOUT}).",
    )
    submit_group.add_argument(
        "--user-agent",
        type=str,
        default=DEFAULT_USER_AGENT,
        help="Custom User-Agent header.",
    )
    submit_group.add_argument(
        "--output",
        type=str,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for output files (default: current directory).",
    )
    submit_group.add_argument(
        "--max-urls",
        type=int,
        default=None,
        help="Maximum number of URLs to process.",
    )

    # ---- Modes ----
    mode_group = parser.add_argument_group("Modes")
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and display URLs without submitting.",
    )
    mode_group.add_argument(
        "--resume",
        action="store_true",
        help="Skip URLs already successfully archived.",
    )
    mode_group.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip URLs that already have a Wayback snapshot.",
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    return parser


def resolve_urls(
    raw_args: list[str],
    crawl: bool = False,
    crawl_depth: int = DEFAULT_CRAWL_DEPTH,
    timeout: int = DEFAULT_TIMEOUT,
    user_agent: str = DEFAULT_USER_AGENT,
    save_urls_file: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> list[str]:
    """Determine whether the positional args are files, URLs, or crawl targets."""
    urls: list[str] = []

    for arg in raw_args:
        if os.path.isfile(arg):
            # It's a file -- load URLs from it
            urls.extend(load_urls(arg))
        elif arg.startswith("http://") or arg.startswith("https://"):
            if crawl:
                # Crawl the site to discover pages
                discovered = crawl_site_urls(
                    arg,
                    max_depth=crawl_depth,
                    timeout=timeout,
                    user_agent=user_agent,
                    logger=logger,
                )
                urls.extend(discovered)
            else:
                urls.append(arg)
        else:
            print(f"WARNING: '{arg}' is not a URL and not a readable file. Skipping.",
                  file=sys.stderr)

    # Save discovered URLs to file if requested
    if save_urls_file and urls:
        with open(save_urls_file, "w", encoding="utf-8") as fh:
            for u in urls:
                fh.write(u + "\n")
        print(f"  Saved {len(urls)} URLs to: {save_urls_file}\n")

    return urls


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Parse CLI args and run the batch archiver."""
    parser = build_parser()
    args = parser.parse_args()

    # Ensure output directory exists
    output_dir = args.output
    os.makedirs(output_dir, exist_ok=True)

    # Set up logging
    log_path = os.path.join(output_dir, "wayback_archiver.log")
    logger = setup_logging(log_path)
    logger.info("=" * 50)
    logger.info("Wayback Bulk Archiver v%s started", __version__)
    logger.info("Arguments: %s", {k: v for k, v in vars(args).items() if k != "secret_key"})

    # Resolve URLs (with optional crawling)
    raw_urls = resolve_urls(
        args.urls,
        crawl=args.crawl,
        crawl_depth=args.crawl_depth,
        timeout=args.timeout,
        user_agent=args.user_agent,
        save_urls_file=args.save_urls,
        logger=logger,
    )

    if not raw_urls:
        print("ERROR: No URLs provided or found.", file=sys.stderr)
        if not args.crawl:
            print("\nTIP: Use --crawl to auto-discover pages on a website:", file=sys.stderr)
            print("  python wayback_bulk_archiver.py https://example.com --crawl", file=sys.stderr)
        sys.exit(1)

    urls = deduplicate_urls(raw_urls)
    logger.info("Loaded %d URLs (%d after dedup).", len(raw_urls), len(urls))

    if len(raw_urls) != len(urls):
        print(f"  Note: {len(raw_urls) - len(urls)} duplicate URL(s) removed.\n")

    # Run
    try:
        run_batch(
            urls,
            delay=args.delay,
            retries=args.retries,
            timeout=args.timeout,
            user_agent=args.user_agent,
            output_dir=output_dir,
            dry_run=args.dry_run,
            resume=args.resume,
            skip_existing=args.skip_existing,
            max_urls=args.max_urls,
            access_key=args.access_key,
            secret_key=args.secret_key,
            logger=logger,
        )
    except KeyboardInterrupt:
        print("\n\n  Interrupted by user (Ctrl+C). Partial results saved.")
        logger.warning("Interrupted by user.")
        sys.exit(130)
    except Exception as exc:
        logger.exception("Fatal error: %s", exc)
        print(f"\nFATAL ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    logger.info("Finished.")


if __name__ == "__main__":
    main()
