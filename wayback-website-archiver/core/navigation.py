"""Navigation and menu extractor for websites (WordPress, Elementor, Gutenberg, standard HTML)."""

from __future__ import annotations

import logging
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup

from core.url_filter import resolve_url, filter_and_deduplicate

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


def extract_links_from_html(
    html: str,
    base_url: str,
    menu_only: bool = False,
    include_external: bool = False,
) -> list[str]:
    """Extract links from HTML string.
    
    If menu_only is True, prioritizes <nav>, <header>, menus, Elementor widgets,
    WordPress navigation structures.
    """
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    target_elements = []

    if menu_only:
        # Search specifically for navigation landmarks and menu structures
        selectors = [
            "nav",
            "header",
            "[role='navigation']",
            "[role='menubar']",
            ".menu",
            ".nav",
            ".navbar",
            ".navigation",
            ".main-navigation",
            ".primary-menu",
            ".nav-menu",
            ".elementor-nav-menu",
            ".elementor-widget-nav-menu",
            ".wp-block-navigation",
            ".wp-block-pages-list",
            "#menu",
            "#main-menu",
            "#site-navigation",
            "#primary-menu",
        ]
        found_sections = set()
        for sel in selectors:
            for el in soup.select(sel):
                if el not in found_sections:
                    found_sections.add(el)
                    target_elements.append(el)

        # If no explicit navigation containers were found, fall back to whole body
        if not target_elements:
            target_elements = [soup]
    else:
        target_elements = [soup]

    raw_urls: list[str] = []
    base_domain = urlparse(base_url).netloc

    for container in target_elements:
        for a_tag in container.find_all("a", href=True):
            href = a_tag["href"].strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            resolved = resolve_url(href, base_url)
            if resolved:
                raw_urls.append(resolved)

    return filter_and_deduplicate(raw_urls, base_domain=base_domain, include_external=include_external)


def fetch_and_extract_menu_urls(
    website_url: str,
    timeout: int = 30,
    include_external: bool = False,
) -> tuple[list[str], str]:
    """Fetch website homepage and extract navigation/menu links.
    
    Returns (urls_list, error_message).
    """
    try:
        session = requests.Session()
        session.headers.update(DEFAULT_HEADERS)
        response = session.get(website_url, timeout=timeout, allow_redirects=True)
        response.raise_for_status()
        
        final_url = response.url
        urls = extract_links_from_html(
            response.text,
            final_url,
            menu_only=True,
            include_external=include_external,
        )
        # Ensure homepage itself is included
        if final_url not in urls and website_url not in urls:
            urls.insert(0, final_url)
        return urls, ""
    except requests.exceptions.RequestException as e:
        logger.warning(f"Error fetching navigation from {website_url}: {e}")
        return [], str(e)
