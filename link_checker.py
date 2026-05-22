import re
import logging
import asyncio
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx

from config import settings

logger = logging.getLogger("maintenance-agent.links")

SKIP_DOMAINS = [
    "amazon.co.uk", "amazon.com", "amzn.to", "ebay.com", "ebay.co.uk",
    "www.amazon.co.uk", "www.amazon.com", "www.ebay.com", "www.ebay.co.uk",
]

WP_AUTH = (settings.WP_USER, settings.WP_APP_PASSWORD)


def _should_skip(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        return any(host == d or host.endswith("." + d) for d in SKIP_DOMAINS)
    except Exception:
        return False


def _extract_links(html: str, base_url: str) -> list[dict]:
    """Extract all <a href> links from HTML content."""
    links = []
    for m in re.finditer(r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.S | re.I):
        href = m.group(1).strip()
        text = re.sub(r'<[^>]+>', '', m.group(2)).strip()
        if href.startswith(('#', 'mailto:', 'tel:', 'javascript:')):
            continue
        full_url = urljoin(base_url, href)
        links.append({"url": full_url, "text": text[:100]})
    return links


async def _check_url(client: httpx.AsyncClient, url: str) -> dict:
    """Check a single URL, HEAD first then GET fallback."""
    if _should_skip(url):
        return {"url": url, "status": "skipped", "code": 0}
    try:
        resp = await client.head(url, timeout=15, follow_redirects=True)
        if resp.status_code >= 400:
            resp = await client.get(url, timeout=15, follow_redirects=True)
        return {"url": url, "status": "ok" if resp.status_code < 400 else "broken", "code": resp.status_code}
    except httpx.TimeoutException:
        return {"url": url, "status": "timeout", "code": 0}
    except Exception as e:
        return {"url": url, "status": "error", "code": 0, "error": str(e)[:200]}


async def _fetch_all_posts_pages(client: httpx.AsyncClient) -> list[dict]:
    """Fetch all published posts and pages from WP REST API."""
    items = []
    for endpoint in ["posts", "pages"]:
        page = 1
        while True:
            try:
                resp = await client.get(
                    f"{settings.WP_URL}/wp-json/wp/v2/{endpoint}",
                    params={"per_page": 50, "page": page, "status": "publish"},
                    auth=WP_AUTH,
                    timeout=30,
                )
                if resp.status_code != 200:
                    break
                data = resp.json()
                if not data:
                    break
                for item in data:
                    items.append({
                        "id": item["id"],
                        "title": item.get("title", {}).get("rendered", ""),
                        "url": item.get("link", ""),
                        "content": item.get("content", {}).get("rendered", ""),
                        "type": endpoint.rstrip("s"),
                    })
                if len(data) < 50:
                    break
                page += 1
            except Exception as e:
                logger.error(f"Failed to fetch {endpoint} page {page}: {e}")
                break
    return items


async def scan_all_links() -> dict:
    """Crawl all published pages/posts, extract and check all links."""
    started = datetime.now(timezone.utc)
    broken_links = []
    total_checked = 0
    skipped = 0
    pages_scanned = 0

    async with httpx.AsyncClient(
        headers={"User-Agent": "PetHubMaintenanceBot/1.0"},
        verify=False,
    ) as client:
        items = await _fetch_all_posts_pages(client)
        pages_scanned = len(items)

        # Collect all unique links with their source
        all_links: dict[str, list[dict]] = {}
        for item in items:
            links = _extract_links(item["content"], item["url"])
            for link in links:
                url = link["url"]
                if url not in all_links:
                    all_links[url] = []
                all_links[url].append({
                    "page_id": item["id"],
                    "page_title": item["title"],
                    "page_url": item["url"],
                    "link_text": link["text"],
                })

        # Check links in batches of 10
        urls = list(all_links.keys())
        for i in range(0, len(urls), 10):
            batch = urls[i:i+10]
            results = await asyncio.gather(*[_check_url(client, u) for u in batch])
            for result in results:
                total_checked += 1
                if result["status"] == "skipped":
                    skipped += 1
                    continue
                if result["status"] != "ok":
                    for source in all_links[result["url"]]:
                        broken_links.append({
                            "url": result["url"],
                            "status_code": result["code"],
                            "status": result["status"],
                            "error": result.get("error", ""),
                            "source_page": source["page_title"],
                            "source_url": source["page_url"],
                            "source_id": source["page_id"],
                            "link_text": source["link_text"],
                            "is_internal": urlparse(result["url"]).hostname in (
                                urlparse(settings.WP_URL).hostname, None
                            ),
                        })
            await asyncio.sleep(0.3)

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()

    return {
        "scanned_at": started.isoformat(),
        "elapsed_seconds": round(elapsed, 1),
        "pages_scanned": pages_scanned,
        "total_links_checked": total_checked,
        "skipped_domains": skipped,
        "broken_count": len(broken_links),
        "broken_links": broken_links,
    }


async def suggest_redirects(broken_links: list) -> list:
    """For broken internal links, try to find the correct URL by slug matching."""
    suggestions = []
    internal = [bl for bl in broken_links if bl.get("is_internal")]
    if not internal:
        return suggestions

    async with httpx.AsyncClient(verify=False) as client:
        items = await _fetch_all_posts_pages(client)

    existing_slugs = {}
    for item in items:
        slug = urlparse(item["url"]).path.rstrip("/").split("/")[-1]
        existing_slugs[slug] = item["url"]

    for bl in internal:
        broken_slug = urlparse(bl["url"]).path.rstrip("/").split("/")[-1]
        # Exact slug match
        if broken_slug in existing_slugs:
            suggestions.append({
                "broken_url": bl["url"],
                "suggested_url": existing_slugs[broken_slug],
                "match_type": "exact_slug",
                "confidence": "high",
            })
            continue
        # Partial match
        for slug, url in existing_slugs.items():
            if broken_slug in slug or slug in broken_slug:
                suggestions.append({
                    "broken_url": bl["url"],
                    "suggested_url": url,
                    "match_type": "partial_slug",
                    "confidence": "medium",
                })
                break

    return suggestions


async def fix_broken_links(broken_links: list) -> dict:
    """Replace broken internal links in content via WP REST API where we have a redirect suggestion."""
    suggestions = await suggest_redirects(broken_links)
    if not suggestions:
        return {"fixed": 0, "message": "No fixable links found"}

    redirect_map = {s["broken_url"]: s["suggested_url"] for s in suggestions if s["confidence"] == "high"}
    if not redirect_map:
        return {"fixed": 0, "suggestions": suggestions, "message": "No high-confidence fixes available"}

    fixed = 0
    errors = []

    async with httpx.AsyncClient(verify=False) as client:
        items = await _fetch_all_posts_pages(client)

        for item in items:
            content = item["content"]
            changed = False
            for broken_url, new_url in redirect_map.items():
                if broken_url in content:
                    content = content.replace(broken_url, new_url)
                    changed = True

            if changed:
                endpoint = "posts" if item["type"] == "post" else "pages"
                try:
                    resp = await client.post(
                        f"{settings.WP_URL}/wp-json/wp/v2/{endpoint}/{item['id']}",
                        json={"content": content},
                        auth=WP_AUTH,
                        timeout=30,
                    )
                    if resp.status_code == 200:
                        fixed += 1
                    else:
                        errors.append(f"Failed to update {item['title']}: HTTP {resp.status_code}")
                except Exception as e:
                    errors.append(f"Error updating {item['title']}: {e}")

    return {
        "fixed": fixed,
        "redirect_map": redirect_map,
        "errors": errors,
    }
