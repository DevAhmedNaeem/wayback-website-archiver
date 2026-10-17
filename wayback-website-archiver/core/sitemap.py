"""Sitemap discovery and parsing (robots.txt, XML sitemaps, sitemap indexes, WordPress sitemaps)."""

from __future__ import annotations

import gzip
import io
import logging
import re
import subprocess
from typing import Optional
from urllib.parse import urljoin, urlparse
import requests
import urllib3
from bs4 import BeautifulSoup

from core.url_filter import filter_and_deduplicate, resolve_url

urllib3.disable_warnings()
logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Keywords identifying non-page sitemaps to exclude (KML location files, attachments/images/media)
NON_PAGE_SITEMAP_KEYWORDS = [
    ".kml",
    "locations.kml",
    "attachment-sitemap",
    "image-sitemap",
    "video-sitemap",
]


def is_non_page_sitemap(url: str) -> bool:
    """Return True if the sitemap URL points to KML files, images, or media attachments."""
    lower = url.lower()
    return any(kw in lower for kw in NON_PAGE_SITEMAP_KEYWORDS)


def find_sitemap_candidates(base_url: str, session: requests.Session, timeout: int = 20) -> list[str]:
    """Find potential sitemap URLs from user input, robots.txt, and standard SEO conventions."""
    parsed = urlparse(base_url)
    root_origin = f"{parsed.scheme}://{parsed.netloc}"
    candidates: list[str] = []

    # 1. If user entered a direct XML sitemap URL (e.g. /page-sitemap.xml or /sitemap.xml), return ONLY that sitemap
    if base_url.lower().endswith((".xml", ".xml.gz")):
        return [base_url]

    if "sitemap" in base_url.lower():
        candidates.append(base_url)

    # 2. Check robots.txt
    robots_url = urljoin(root_origin, "/robots.txt")
    try:
        resp = session.get(robots_url, timeout=min(timeout, 3), verify=False)
        if resp.status_code == 200:
            for line in resp.text.splitlines():
                line = line.strip()
                if line.lower().startswith("sitemap:"):
                    parts = line.split(":", 1)
                    if len(parts) > 1:
                        sitemap_url = parts[1].strip()
                        if sitemap_url and not is_non_page_sitemap(sitemap_url) and sitemap_url not in candidates:
                            candidates.append(sitemap_url)
    except Exception as e:
        logger.debug(f"Failed to read robots.txt from {robots_url}: {e}")

    # 3. Standard sitemap paths across Yoast, RankMath, WP Core, AIOSEO, SEOPress, Google XML
    defaults = [
        "/sitemap_index.xml",
        "/sitemap.xml",
        "/wp-sitemap.xml",
        "/page-sitemap.xml",
        "/pages-sitemap.xml",
        "/sitemap-pages.xml",
        "/post-sitemap.xml",
        "/wp-sitemap-posts-page-1.xml",
        "/wp-sitemap-posts-post-1.xml",
        "/root-sitemap.xml",
        "/sitemaps.xml",
        "/sitemap/",
    ]
    for path in defaults:
        url = urljoin(root_origin, path)
        if url not in candidates:
            candidates.append(url)

    return candidates


def parse_xml_content(content_bytes: bytes) -> str:
    """Detect if content is gzipped and decompress, or decode to string."""
    try:
        if content_bytes[:2] == b"\x1f\x8b":
            with gzip.GzipFile(fileobj=io.BytesIO(content_bytes)) as gz:
                return gz.read().decode("utf-8", errors="replace")
    except Exception:
        pass
    return content_bytes.decode("utf-8", errors="replace")


def extract_loc_from_sitemap(
    xml_text: str,
) -> tuple[list[str], list[str]]:
    """Parse XML sitemap text.
    
    Returns (page_urls, child_sitemap_urls).
    Excludes .kml location files and non-page resources.
    """
    page_urls: list[str] = []
    child_sitemaps: list[str] = []

    try:
        soup = BeautifulSoup(xml_text, "xml")
    except Exception:
        try:
            soup = BeautifulSoup(xml_text, "html.parser")
        except Exception:
            return [], []

    # Check for sitemap index
    sitemap_tags = soup.find_all(["sitemap", "SITEMAP"])
    if sitemap_tags:
        for sm in sitemap_tags:
            loc = sm.find(["loc", "LOC"])
            if loc and loc.text:
                child_url = loc.text.strip()
                if not is_non_page_sitemap(child_url):
                    child_sitemaps.append(child_url)

    # Check for urlset
    url_tags = soup.find_all(["url", "URL"])
    if url_tags:
        for u in url_tags:
            loc = u.find(["loc", "LOC"])
            if loc and loc.text:
                loc_str = loc.text.strip()
                if loc_str.lower().endswith((".kml", ".xml", ".xml.gz")) or ".kml" in loc_str.lower():
                    continue
                page_urls.append(loc_str)

    # Fallback regex if soup missed due to custom tags or formatting
    if not page_urls and not child_sitemaps:
        locs = re.findall(r"<loc>(https?://[^<]+)</loc>", xml_text, re.IGNORECASE)
        for loc in locs:
            loc = loc.strip()
            if loc.lower().endswith((".kml", ".kml/")) or ".kml" in loc.lower():
                continue
            if "sitemap" in loc.lower() and loc.endswith(".xml"):
                if not is_non_page_sitemap(loc):
                    child_sitemaps.append(loc)
            elif not loc.lower().endswith((".xml", ".xml.gz")):
                page_urls.append(loc)

    return page_urls, child_sitemaps


def make_raw_wayback_url(archive_url: str) -> str:
    """Convert a standard Wayback URL (e.g. /web/20261017150000/https://...) to raw replay (/web/20261017150000id_/https://...)."""
    if "/web/" in archive_url:
        prefix, rest = archive_url.split("/web/", 1)
        slash_pos = rest.find("/")
        if slash_pos != -1:
            timestamp = rest[:slash_pos]
            target = rest[slash_pos:]
            if not timestamp.endswith("id_"):
                return f"{prefix}/web/{timestamp}id_{target}"
    return archive_url


def fetch_url_bytes(url: str, timeout: int = 15) -> Optional[bytes]:
    """Fetch URL bytes reliably on Windows using native curl with IPv4 or requests fallback."""
    try:
        proc = subprocess.run(
            ["curl.exe", "-4", "-k", "-s", "-L", "--max-time", str(timeout), url],
            capture_output=True,
            timeout=timeout + 3,
        )
        if proc.returncode == 0 and len(proc.stdout) > 0:
            return proc.stdout
    except Exception:
        pass

    try:
        resp = requests.get(url, timeout=timeout, verify=False)
        if resp.status_code == 200 and resp.content:
            return resp.content
    except Exception:
        pass
    return None


def fetch_sitemap_xml(
    sm_url: str,
    session: requests.Session,
    timeout: int = 10,
) -> tuple[bytes | None, str]:
    """Fetch sitemap XML bytes.
    
    1. First tries direct HTTP GET with standard headers and SSL verification disabled.
    2. If direct fetch fails, tries Windows curl IPv4.
    3. If direct fetch blocks or fails, checks Wayback Machine snapshot (/web/2id_/...).
    4. If not available, uses Wayback SPN proxy to archive and retrieve without bot blocks.
    """
    # 1. Direct fetch attempt
    try:
        resp = session.get(sm_url, timeout=timeout, verify=False)
        if resp.status_code in (404, 410):
            return None, "not_found"
        if resp.status_code == 200 and resp.content:
            lowered = resp.content[:350].lower()
            is_bot_blocked = any(m in lowered for m in (
                b"one moment, please",
                b"please wait while your request is being verified",
                b"cf-browser-verification",
                b"just a moment...",
                b"wsidchk",
                b"attention required!",
            ))
            if not is_bot_blocked:
                content_type = resp.headers.get("content-type", "").lower()
                if "xml" in content_type or sm_url.endswith((".xml", ".xml.gz")):
                    return resp.content, "direct"
                if b"<?xml" in lowered or b"<sitemap" in lowered or b"<urlset" in lowered:
                    return resp.content, "direct"
            else:
                logger.info(f"Sitemap {sm_url} blocked by bot protection. Trying curl / Wayback fallback...")
    except Exception as e:
        logger.debug(f"Direct sitemap fetch failed for {sm_url}: {e}")

    # 1b. Try curl.exe -4
    raw_curl = fetch_url_bytes(sm_url, timeout=timeout)
    if raw_curl:
        lowered = raw_curl[:350].lower()
        if b"<?xml" in lowered or b"<sitemap" in lowered or b"<urlset" in lowered:
            return raw_curl, "direct_curl"

    # 2. Check if Wayback Machine already has a snapshot of this sitemap
    try:
        wayback_latest = f"https://web.archive.org/web/2id_/{sm_url}"
        raw_wb = fetch_url_bytes(wayback_latest, timeout=12)
        if raw_wb:
            decomp = parse_xml_content(raw_wb)
            if "<loc>" in decomp or "<LOC>" in decomp:
                logger.info(f"Loaded existing sitemap snapshot from Wayback: {wayback_latest}")
                return raw_wb, "wayback_existing"
    except Exception as e:
        logger.debug(f"Wayback existing snapshot check failed: {e}")

    # 3. Wayback Machine SPN Proxy Fallback
    try:
        from core.settings import load_settings
        from core.wayback import WaybackClient

        settings = load_settings()
        if settings.has_credentials:
            logger.info(f"Activating Wayback Machine proxy capture for sitemap: {sm_url}")
            client = WaybackClient(
                access_key=settings.access_key,
                secret_key=settings.secret_key,
                request_timeout=25,
            )
            client.session.verify = False

            res = client.archive_url(
                url=sm_url,
                check_existing=False,
                skip_existing_days=0,
                capture_all=False,
                max_poll_seconds=45,
            )
            if res.status == "SUCCESS" and res.archive_url:
                raw_url = make_raw_wayback_url(res.archive_url)
                logger.info(f"Retrieved sitemap snapshot via Wayback: {raw_url}")
                raw_bytes = fetch_url_bytes(raw_url, timeout=15)
                if raw_bytes:
                    return raw_bytes, "wayback_proxy"
    except Exception as err:
        logger.warning(f"Wayback proxy fallback failed for {sm_url}: {err}")

    return None, "failed"


def discover_sitemap_urls(
    website_url: str,
    timeout: int = 30,
    max_sitemaps: int = 50,
    include_external: bool = False,
) -> tuple[list[str], list[str]]:
    """Discover all page URLs from site sitemaps recursively across the entire site.
    
    CRAWLS ALL CHILD SITEMAPS IN THE INDEX so no pages are missed!
    Returns (all_discovered_urls, sitemaps_found).
    """
    session = requests.Session()
    session.verify = False
    session.headers.update(DEFAULT_HEADERS)
    base_domain = urlparse(website_url).netloc

    candidates = find_sitemap_candidates(website_url, session, timeout=min(timeout, 8))
    visited_sitemaps: set[str] = set()
    sitemaps_to_crawl: list[str] = list(candidates)
    sitemaps_found: list[str] = []
    all_page_urls: list[str] = []
    found_sitemap_index = False

    while sitemaps_to_crawl and len(visited_sitemaps) < max_sitemaps:
        sm_url = sitemaps_to_crawl.pop(0)
        if sm_url in visited_sitemaps:
            continue
        visited_sitemaps.add(sm_url)

        # Skip non-page files like KML or media attachments
        if is_non_page_sitemap(sm_url):
            logger.info(f"Skipping non-page sitemap: {sm_url}")
            continue

        try:
            content_bytes, method = fetch_sitemap_xml(sm_url, session, timeout=5)
            if not content_bytes:
                continue

            sitemaps_found.append(sm_url)
            xml_text = parse_xml_content(content_bytes)
            pages, children = extract_loc_from_sitemap(xml_text)

            if pages:
                all_page_urls.extend(pages)

            if children:
                found_sitemap_index = True
                for child in children:
                    if is_non_page_sitemap(child):
                        continue
                    if child not in visited_sitemaps and child not in sitemaps_to_crawl:
                        sitemaps_to_crawl.append(child)

        except Exception as e:
            logger.debug(f"Failed to process sitemap {sm_url}: {e}")

    filtered = filter_and_deduplicate(
        all_page_urls,
        base_domain=base_domain,
        include_external=include_external,
    )
    return filtered, sitemaps_found
