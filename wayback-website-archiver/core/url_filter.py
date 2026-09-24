"""URL filtering, normalization, and deduplication."""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse, urlunparse, urljoin


# Extensions to skip (non-page resources)
SKIP_EXTENSIONS: set[str] = {
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".bmp", ".tiff",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".rar", ".gz", ".tar", ".7z", ".bz2",
    ".mp3", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".ogg", ".wav",
    ".css", ".js", ".json", ".xml", ".rss", ".atom", ".kml",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".exe", ".dmg", ".msi", ".apk",
}

# Path segments to skip by default
SKIP_PATH_SEGMENTS: list[str] = [
    "/wp-admin", "/wp-login", "/wp-content/", "/wp-includes/",
    "/wp-json/", "/xmlrpc", "/feed/", "/feed$",
    "/cart", "/checkout", "/my-account", "/account",
    "/login", "/logout", "/register", "/signup",
    "?add-to-cart", "?action=", "/trackback/",
    "/wp-cron", "/.well-known/",
]

# Domains to treat as external/social
SOCIAL_DOMAINS: set[str] = {
    "facebook.com", "www.facebook.com", "fb.com",
    "twitter.com", "www.twitter.com", "x.com",
    "instagram.com", "www.instagram.com",
    "youtube.com", "www.youtube.com",
    "linkedin.com", "www.linkedin.com",
    "pinterest.com", "www.pinterest.com",
    "tiktok.com", "www.tiktok.com",
    "google.com", "www.google.com", "maps.google.com",
    "yelp.com", "www.yelp.com",
    "apple.com", "apps.apple.com",
    "play.google.com",
}


def normalize_url(url: str) -> str:
    """Normalize a URL for comparison and deduplication.

    Lowercases scheme and host.  Removes default ports.
    Removes fragments.  Preserves path, query string, and trailing slashes.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return url

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()

    # Remove default ports
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    elif netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]

    # Rebuild without fragment
    return urlunparse((scheme, netloc, parsed.path, parsed.params, parsed.query, ""))


def is_same_domain(url: str, base_domain: str) -> bool:
    """Check if a URL belongs to the same domain (treats www/non-www as equal)."""
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower().split(":")[0]
    except Exception:
        return False

    base = base_domain.lower().split(":")[0]

    # Strip www. from both
    host_clean = host.lstrip("www.")
    base_clean = base.lstrip("www.")

    return host_clean == base_clean


def get_base_domain(url: str) -> str:
    """Extract the base domain from a URL."""
    try:
        parsed = urlparse(url)
        return parsed.netloc.lower()
    except Exception:
        return ""


def resolve_url(href: str, base_url: str) -> str:
    """Resolve a relative URL against a base URL."""
    if not href:
        return ""
    try:
        return urljoin(base_url, href)
    except Exception:
        return ""


def should_skip_url(url: str, include_external: bool = False) -> tuple[bool, str]:
    """Determine if a URL should be skipped.

    Returns (should_skip, reason).
    """
    if not url:
        return True, "Empty URL"

    lower = url.lower().strip()

    # Skip non-HTTP schemes
    if lower.startswith(("mailto:", "tel:", "javascript:", "data:", "ftp:", "file:")):
        return True, f"Non-HTTP scheme"

    # Skip bare anchors
    if lower.startswith("#"):
        return True, "Anchor only"

    try:
        parsed = urlparse(url)
    except Exception:
        return True, "Malformed URL"

    if parsed.scheme and parsed.scheme not in ("http", "https"):
        return True, f"Unsupported scheme: {parsed.scheme}"

    # Skip known file extensions
    path_lower = parsed.path.lower()
    ext = os.path.splitext(path_lower)[1]
    if ext in SKIP_EXTENSIONS:
        return True, f"Non-page resource ({ext})"

    # Skip admin/internal paths
    for seg in SKIP_PATH_SEGMENTS:
        if seg in path_lower:
            return True, f"Internal/admin path ({seg})"

    # Skip social/external domains (if not including external)
    if not include_external and parsed.netloc:
        host = parsed.netloc.lower().split(":")[0]
        if host in SOCIAL_DOMAINS:
            return True, f"Social/external domain ({host})"

    return False, ""


def filter_and_deduplicate(
    urls: list[str],
    base_domain: str,
    include_external: bool = False,
) -> list[str]:
    """Filter, normalize, and deduplicate a list of URLs.

    Returns only valid, internal (unless include_external), unique URLs.
    """
    seen: set[str] = set()
    result: list[str] = []

    for url in urls:
        skip, reason = should_skip_url(url, include_external)
        if skip:
            continue

        # Domain check
        if not include_external:
            try:
                parsed = urlparse(url)
                if parsed.netloc and not is_same_domain(url, base_domain):
                    continue
            except Exception:
                continue

        norm = normalize_url(url)
        if norm and norm not in seen:
            seen.add(norm)
            result.append(url)

    return result
