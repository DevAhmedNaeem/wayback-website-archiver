"""Crawl orchestrator combining navigation, sitemap, and BFS internal link crawling."""

from __future__ import annotations

import logging
from collections import deque
from typing import Callable, Optional
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup

from core.navigation import extract_links_from_html, fetch_and_extract_menu_urls, DEFAULT_HEADERS
from core.sitemap import discover_sitemap_urls
from core.url_filter import filter_and_deduplicate, is_same_domain, normalize_url, should_skip_url

logger = logging.getLogger(__name__)


class WebsiteCrawler:
    """Orchestrates website page discovery."""

    def __init__(
        self,
        base_url: str,
        max_pages: int = 500,
        max_depth: int = 3,
        check_sitemap: bool = True,
        check_navigation: bool = True,
        follow_internal_links: bool = True,
        include_external: bool = False,
        timeout: int = 30,
        status_callback: Optional[Callable[[dict], None]] = None,
    ):
        self.base_url = base_url.strip()
        if not self.base_url.startswith(("http://", "https://")):
            self.base_url = "https://" + self.base_url

        self.max_pages = max_pages
        self.max_depth = max_depth
        self.check_sitemap = check_sitemap
        self.check_navigation = check_navigation
        self.follow_internal_links = follow_internal_links
        self.include_external = include_external
        self.timeout = timeout
        self.status_callback = status_callback

        self.base_domain = urlparse(self.base_url).netloc
        self.discovered_urls: list[str] = []
        self.discovered_set: set[str] = set()
        self.logs: list[str] = []
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

    def _log(self, message: str) -> None:
        logger.info(message)
        self.logs.append(message)
        if self.status_callback:
            self.status_callback({
                "type": "log",
                "message": message,
                "count": len(self.discovered_urls),
            })

    def _add_url(self, url: str, source: str = "") -> bool:
        norm = normalize_url(url)
        if not norm or norm in self.discovered_set:
            return False
        
        skip, _ = should_skip_url(url, include_external=self.include_external)
        if skip:
            return False

        if not self.include_external and not is_same_domain(url, self.base_domain):
            return False

        if len(self.discovered_urls) >= self.max_pages:
            return False

        self.discovered_set.add(norm)
        self.discovered_urls.append(url)

        if self.status_callback:
            self.status_callback({
                "type": "url_found",
                "url": url,
                "source": source,
                "count": len(self.discovered_urls),
            })
        return True

    def crawl_sitemap_only(self) -> list[str]:
        """Discover pages strictly using sitemap(s)."""
        self._log(f"Starting sitemap discovery for {self.base_url}...")
        parsed = urlparse(self.base_url)
        homepage = f"{parsed.scheme}://{parsed.netloc}/"
        self._add_url(homepage, source="homepage")
        
        urls, sitemaps = discover_sitemap_urls(
            self.base_url,
            timeout=self.timeout,
            include_external=self.include_external,
        )
        self._log(f"Found {len(sitemaps)} sitemaps, extracting URLs...")
        for u in urls:
            if len(self.discovered_urls) >= self.max_pages:
                break
            self._add_url(u, source="sitemap")

        self._log(f"Sitemap discovery finished: {len(self.discovered_urls)} unique pages.")
        return self.discovered_urls

    def crawl_menu_only(self) -> list[str]:
        """Discover pages strictly from navigation and header menus."""
        parsed = urlparse(self.base_url)
        homepage = f"{parsed.scheme}://{parsed.netloc}/"
        self._log(f"Starting menu/navigation discovery for {homepage}...")
        self._add_url(homepage, source="homepage")

        menu_urls, err = fetch_and_extract_menu_urls(
            homepage,
            timeout=self.timeout,
            include_external=self.include_external,
        )
        if err:
            self._log(f"Navigation fetch warning: {err}")

        for u in menu_urls:
            if len(self.discovered_urls) >= self.max_pages:
                break
            self._add_url(u, source="menu")

        self._log(f"Menu discovery finished: {len(self.discovered_urls)} unique pages.")
        return self.discovered_urls

    def crawl_smart(self) -> list[str]:
        """Smart discovery: Navigation + Sitemap + Internal link crawler."""
        self._log(f"Starting Smart Discovery for {self.base_url}...")
        self._add_url(self.base_url, source="homepage")

        # 1. Menu links
        if self.check_navigation:
            self._log("Scanning navigation and menu elements...")
            menu_urls, err = fetch_and_extract_menu_urls(
                self.base_url,
                timeout=self.timeout,
                include_external=self.include_external,
            )
            for u in menu_urls:
                self._add_url(u, source="menu")
            self._log(f"Discovered {len(self.discovered_urls)} pages after navigation scan.")

        # 2. Sitemap
        if self.check_sitemap and len(self.discovered_urls) < self.max_pages:
            self._log("Checking sitemap.xml and robots.txt...")
            sitemap_urls, sitemaps = discover_sitemap_urls(
                self.base_url,
                timeout=self.timeout,
                include_external=self.include_external,
            )
            for u in sitemap_urls:
                if len(self.discovered_urls) >= self.max_pages:
                    break
                self._add_url(u, source="sitemap")
            self._log(f"Discovered {len(self.discovered_urls)} pages after sitemap scan.")

        # 3. BFS Internal link crawling
        if self.follow_internal_links and len(self.discovered_urls) < self.max_pages and self.max_depth > 0:
            self._log(f"Starting BFS internal link crawl (max depth: {self.max_depth}, max pages: {self.max_pages})...")
            
            # Queue holds (url, current_depth)
            queue: deque[tuple[str, int]] = deque()
            visited_pages: set[str] = set()

            # Seed with current discovered pages
            for u in list(self.discovered_urls):
                queue.append((u, 1))

            while queue and len(self.discovered_urls) < self.max_pages:
                current_url, depth = queue.popleft()
                norm_curr = normalize_url(current_url)
                if norm_curr in visited_pages:
                    continue
                visited_pages.add(norm_curr)

                if depth > self.max_depth:
                    continue

                try:
                    resp = self.session.get(current_url, timeout=self.timeout, allow_redirects=True)
                    if resp.status_code != 200:
                        continue
                    
                    content_type = resp.headers.get("content-type", "").lower()
                    if "text/html" not in content_type:
                        continue

                    # Extract all page links
                    page_links = extract_links_from_html(
                        resp.text,
                        resp.url,
                        menu_only=False,
                        include_external=self.include_external,
                    )

                    for link in page_links:
                        if len(self.discovered_urls) >= self.max_pages:
                            break
                        if self._add_url(link, source="internal_link"):
                            if depth + 1 <= self.max_depth:
                                queue.append((link, depth + 1))

                except Exception as e:
                    logger.debug(f"Failed to crawl {current_url}: {e}")

        self._log(f"Discovery complete. Total unique pages found: {len(self.discovered_urls)}")
        return self.discovered_urls
