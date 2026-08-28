"""Small, reusable parsers for source listing pages."""

from __future__ import annotations

import html
import re
from urllib.parse import urljoin


def parse_li_list(page_html: str, base_url: str, region: str, link_pattern: str) -> list[dict]:
    """Parse list anchors matching an explicit source-scoped URL pattern."""
    items: list[dict] = []
    seen: set[str] = set()
    pattern = re.compile(link_pattern, re.I)
    for match in re.finditer(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page_html, re.I | re.S):
        href, inner = match.group(1), match.group(2)
        if not pattern.search(href):
            continue
        title = html.unescape(re.sub(r"<[^>]+>", "", inner)).strip()
        title = re.sub(r"\s+", " ", title)
        title = re.sub(r"\s*(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})\s*$", "", title).strip()
        if len(title) < 6:
            continue
        source_url = urljoin(base_url, href)
        if source_url in seen:
            continue
        seen.add(source_url)
        block = page_html[max(0, match.start() - 400): match.end() + 400]
        date_match = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", block)
        published_at = (
            f"{date_match.group(1)}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}"
            if date_match else ""
        )
        items.append({"source_url": source_url, "title": title, "published_at": published_at,
                      "buyer": "", "region": region, "content": ""})
    return items
