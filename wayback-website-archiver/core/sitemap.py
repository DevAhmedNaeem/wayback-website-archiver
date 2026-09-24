"""Sitemap discovery and parsing (robots.txt, XML sitemaps, sitemap indexes, WordPress sitemaps)."""

from __future__ import annotations

import gzip
import io
import logging
import re
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

from core.url_filter import filter_and_deduplicate, resolve_url

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Keywords identifying non-page sitemaps to exclude (posts, tags, authors, categories, KML files)
POST_SITEMAP_KEYWORDS = [
    "post-sitemap",
    "posts-post",
    "post_tag",
    "blog-sitemap",
    "author-sitemap",
    "tag-sitemap",
    "category-sitemap",
    "news-sitemap",
    "local-sitemap",
    ".kml",
    "locations.kml",
    "attachment-sitemap",
]


def is_post_sitemap(url: str) -> bool:
    """Return True if the sitemap URL points to blog posts, tags, authors, or KML files."""
    lower = url.lower()
    return any(kw in lower for kw in POST_SITEMAP_KEYWORDS)


def find_sitemap_candidates(base_url: str, session: requests.Session, timeout: int = 20) -> list[str]:
    """Find potential sitemap URLs from user input, robots.txt, and standard SEO conventions."""
    parsed = urlparse(base_url)
    root_origin = f"{parsed.scheme}://{parsed.netloc}"
    candidates: list[str] = []

    # 1. If user entered a direct XML sitemap URL (e.g. /page-sitemap.xml), return ONLY that sitemap!
    if base_url.lower().endswith((".xml", ".xml.gz")):
        return [base_url]

    if "sitemap" in base_url.lower():
        candidates.append(base_url)

    # 2. Check robots.txt
    robots_url = urljoin(root_origin, "/robots.txt")
    try:
        resp = session.get(robots_url, timeout=timeout)
        if resp.status_code == 200:
            for line in resp.text.splitlines():
                line = line.strip()
                if line.lower().startswith("sitemap:"):
                    parts = line.split(":", 1)
                    if len(parts) > 1:
                        sitemap_url = parts[1].strip()
                        if sitemap_url and not is_post_sitemap(sitemap_url) and sitemap_url not in candidates:
                            candidates.append(sitemap_url)
    except Exception as e:
        logger.debug(f"Failed to read robots.txt from {robots_url}: {e}")

    # 3. Standard sitemap paths across Yoast, RankMath, WP Core, AIOSEO, SEOPress, Google XML
    defaults = [
        "/page-sitemap.xml",
        "/wp-sitemap-posts-page-1.xml",
        "/wp-sitemap-posts-page-2.xml",
        "/pages-sitemap.xml",
        "/sitemap-pages.xml",
        "/sitemap.xml",
        "/sitemap_index.xml",
        "/wp-sitemap.xml",
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
                if not is_post_sitemap(child_url):
                    child_sitemaps.append(child_url)

    # Check for urlset
    url_tags = soup.find_all(["url", "URL"])
    if url_tags:
        for u in url_tags:
            loc = u.find(["loc", "LOC"])
            if loc and loc.text:
                loc_str = loc.text.strip()
                # Skip .kml files or .xml files appearing in urlset
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
                if not is_post_sitemap(loc):
                    child_sitemaps.append(loc)
            elif not loc.lower().endswith((".xml", ".xml.gz")):
                page_urls.append(loc)

    return page_urls, child_sitemaps


def discover_sitemap_urls(
    website_url: str,
    timeout: int = 30,
    max_sitemaps: int = 50,
    include_external: bool = False,
) -> tuple[list[str], list[str]]:
    """Discover all page URLs from site sitemaps recursively.
    
    Supports direct sitemap URLs (e.g. /page-sitemap.xml) and root domains.
    Returns (all_discovered_urls, sitemaps_found).
    """
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    base_domain = urlparse(website_url).netloc

    candidates = find_sitemap_candidates(website_url, session, timeout=timeout)
    visited_sitemaps: set[str] = set()
    sitemaps_to_crawl: list[str] = list(candidates)
    sitemaps_found: list[str] = []
    all_page_urls: list[str] = []

    while sitemaps_to_crawl and len(visited_sitemaps) < max_sitemaps:
        sm_url = sitemaps_to_crawl.pop(0)
        if sm_url in visited_sitemaps:
            continue
        visited_sitemaps.add(sm_url)

        # Skip blog post / article / KML sitemaps completely
        if is_post_sitemap(sm_url):
            logger.info(f"Skipping non-page sitemap: {sm_url}")
            continue

        try:
            resp = session.get(sm_url, timeout=timeout)
            if resp.status_code != 200 or not resp.content:
                continue

            content_type = resp.headers.get("content-type", "").lower()
            if "xml" not in content_type and not sm_url.endswith((".xml", ".xml.gz")):
                snippet = resp.content[:200].lower()
                if b"<?xml" not in snippet and b"<sitemap" not in snippet and b"<urlset" not in snippet:
                    continue

            sitemaps_found.append(sm_url)
            xml_text = parse_xml_content(resp.content)
            pages, children = extract_loc_from_sitemap(xml_text)

            all_page_urls.extend(pages)
            for child in children:
                if is_post_sitemap(child):
                    continue
                if child not in visited_sitemaps and child not in sitemaps_to_crawl:
                    sitemaps_to_crawl.append(child)

        except Exception as e:
            logger.debug(f"Failed to fetch sitemap {sm_url}: {e}")

    filtered = filter_and_deduplicate(
        all_page_urls,
        base_domain=base_domain,
        include_external=include_external,
    )
    return filtered, sitemaps_found
