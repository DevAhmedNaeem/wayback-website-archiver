"""Navigation and menu extractor for websites (WordPress, Elementor, Gutenberg, standard HTML)."""

from __future__ import annotations

import logging
import re
import socket
import subprocess
from urllib.parse import urlparse
import requests
import urllib3
from bs4 import BeautifulSoup

from core.url_filter import resolve_url, filter_and_deduplicate

urllib3.disable_warnings()
logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

WAYBACK_PREFIX_REGEX = re.compile(
    r"^(?:https?://web\.archive\.org)?/web/\d+[a-z_]*/(https?://.+)$",
    re.IGNORECASE,
)
WAYBACK_ID_REGEX = re.compile(
    r"^(?:https?://web\.archive\.org)?/web/[^/]+/(https?://.+)$",
    re.IGNORECASE,
)


def unwrap_wayback_url(href: str) -> str:
    """Unwrap URLs rewritten by Wayback Machine back to original URLs."""
    if not href:
        return ""
    m = WAYBACK_PREFIX_REGEX.match(href)
    if m:
        return m.group(1)
    m2 = WAYBACK_ID_REGEX.match(href)
    if m2:
        return m2.group(1)
    return href


def get_homepage_url(url: str) -> str:
    """Derive clean website homepage root URL from any URL (including sitemaps)."""
    parsed = urlparse(url.strip())
    scheme = parsed.scheme if parsed.scheme in ("http", "https") else "https"
    netloc = parsed.netloc
    return f"{scheme}://{netloc}/"


def extract_links_from_html(
    html: str,
    base_url: str,
    menu_only: bool = False,
    include_external: bool = False,
) -> list[str]:
    """Extract links from HTML string.
    
    If menu_only is True, prioritizes navigation landmarks, headers, menus,
    footers, Elementor widgets, and WordPress nav blocks. If <= 1 link is found from menu elements,
    automatically falls back to extracting all internal links from the entire page so
    the user never gets stuck with just 1 page!
    """
    if not html:
        return []

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    target_elements = []

    if menu_only:
        # Search comprehensively for navigation landmarks, menus, headers, and footers
        selectors = [
            "nav",
            "header",
            "footer",
            "[role='navigation']",
            "[role='menubar']",
            "[role='contentinfo']",
            ".menu",
            ".nav",
            ".navbar",
            ".navigation",
            ".main-navigation",
            ".primary-menu",
            ".nav-menu",
            ".elementor-nav-menu",
            ".elementor-widget-nav-menu",
            ".elementor-widget-theme-site-logo",
            ".wp-block-navigation",
            ".wp-block-pages-list",
            ".site-footer",
            ".footer",
            "#footer",
            "#menu",
            "#main-menu",
            "#site-navigation",
            "#primary-menu",
            "#header",
            ".header",
            ".site-header",
            "#navbar",
            "ul[id*='menu']",
            "ul[class*='menu']",
            "ul[class*='nav']",
            "div[class*='menu']",
            "div[class*='nav']",
            "div[class*='navigation']",
            ".menu-item",
            ".nav-item",
            ".et_pb_menu",
            ".mobile-menu",
        ]
        found_sections = set()
        for sel in selectors:
            for el in soup.select(sel):
                if el not in found_sections:
                    found_sections.add(el)
                    target_elements.append(el)

        if not target_elements:
            target_elements = [soup]
    else:
        target_elements = [soup]

    raw_urls: list[str] = []
    base_domain = urlparse(base_url).netloc

    for container in target_elements:
        for a_tag in container.find_all("a", href=True):
            raw_href = a_tag["href"].strip()
            if not raw_href or raw_href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            unwrapped = unwrap_wayback_url(raw_href)
            resolved = resolve_url(unwrapped, base_url)
            if resolved:
                raw_urls.append(resolved)

    filtered = filter_and_deduplicate(raw_urls, base_domain=base_domain, include_external=include_external)

    # Fail-safe: If menu_only returned 1 or 0 links, extract all valid links from the entire HTML page
    if menu_only and len(filtered) <= 1:
        all_raw_urls: list[str] = []
        for a_tag in soup.find_all("a", href=True):
            raw_href = a_tag["href"].strip()
            if not raw_href or raw_href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            unwrapped = unwrap_wayback_url(raw_href)
            resolved = resolve_url(unwrapped, base_url)
            if resolved:
                all_raw_urls.append(resolved)
        fallback_filtered = filter_and_deduplicate(all_raw_urls, base_domain=base_domain, include_external=include_external)
        if len(fallback_filtered) > len(filtered):
            return fallback_filtered

    return filtered


def fetch_url_html(url: str, timeout: int = 15) -> tuple[str, str]:
    """Fetch HTML with curl IPv4 fallback and full browser headers."""
    # 1. Try python requests
    try:
        session = requests.Session()
        session.verify = False
        session.headers.update(DEFAULT_HEADERS)
        resp = session.get(url, timeout=timeout, allow_redirects=True)
        if resp.status_code == 200 and resp.text:
            return resp.text, resp.url
    except Exception:
        pass

    # 2. Try alternate protocol (https -> http or vice versa)
    alt_url = ""
    if url.startswith("https://"):
        alt_url = "http://" + url[8:]
    elif url.startswith("http://"):
        alt_url = "https://" + url[7:]

    if alt_url:
        try:
            session = requests.Session()
            session.verify = False
            session.headers.update(DEFAULT_HEADERS)
            resp = session.get(alt_url, timeout=timeout, allow_redirects=True)
            if resp.status_code == 200 and resp.text:
                return resp.text, resp.url
        except Exception:
            pass

    # 3. Windows native curl with browser User-Agent
    try:
        proc = subprocess.run(
            [
                "curl.exe",
                "-k",
                "-s",
                "-L",
                "-A", DEFAULT_HEADERS["User-Agent"],
                "--max-time", str(timeout),
                url,
            ],
            capture_output=True,
            timeout=timeout + 3,
        )
        if proc.returncode == 0 and len(proc.stdout) > 0:
            text = proc.stdout.decode("utf-8", errors="replace")
            return text, url
    except Exception:
        pass

    return "", url


def fetch_and_extract_menu_urls(
    website_url: str,
    timeout: int = 30,
    include_external: bool = False,
) -> tuple[list[str], str]:
    """Fetch website homepage and extract navigation and menu links.
    
    If website_url is a sitemap URL or inner page, automatically resolves to the
    root homepage to guarantee menu links are found.
    Returns (urls_list, error_message).
    """
    homepage = get_homepage_url(website_url)
    target_urls_to_try = [homepage]
    
    # If the user passed an inner HTML page (not XML sitemap), also try that page
    if website_url.strip("/") != homepage.strip("/") and not website_url.lower().endswith((".xml", ".xml.gz", ".rss", ".atom")):
        target_urls_to_try.append(website_url)

    all_menu_links: list[str] = []
    last_err = ""

    for target in target_urls_to_try:
        html_text, final_url = fetch_url_html(target, timeout=min(timeout, 15))
        if html_text:
            extracted = extract_links_from_html(
                html_text,
                final_url,
                menu_only=True,
                include_external=include_external,
            )
            for u in extracted:
                if u not in all_menu_links:
                    all_menu_links.append(u)

    if all_menu_links:
        if homepage not in all_menu_links:
            all_menu_links.insert(0, homepage)
        return all_menu_links, ""

    # Wayback Machine fallback snapshot if direct fetch failed
    logger.info(f"Direct fetch for {homepage} gave no links. Trying Wayback snapshot fallback...")
    try:
        for wb_base in (f"http://web.archive.org/web/2/{homepage}", f"https://web.archive.org/web/2/{homepage}"):
            wb_text, _ = fetch_url_html(wb_base, timeout=12)
            if wb_text:
                urls = extract_links_from_html(
                    wb_text,
                    homepage,
                    menu_only=True,
                    include_external=include_external,
                )
                if homepage not in urls:
                    urls.insert(0, homepage)
                if len(urls) > 1:
                    logger.info(f"Successfully extracted {len(urls)} menu links via Wayback snapshot!")
                    return urls, ""
    except Exception as wb_err:
        last_err = str(wb_err)

    return [homepage], last_err

