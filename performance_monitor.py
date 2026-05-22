import re
import time
import logging
import asyncio
from datetime import datetime, timezone

import httpx

from config import settings

logger = logging.getLogger("maintenance-agent.performance")

WP_AUTH = (settings.WP_USER, settings.WP_APP_PASSWORD)

# Thresholds
TTFB_GOOD = 500
TTFB_OK = 1000
SIZE_GOOD = 500_000  # 500KB
SIZE_OK = 1_500_000  # 1.5MB


def _grade_page(ttfb_ms: float, size_bytes: int, lazy_pct: float, has_compression: bool) -> str:
    score = 0
    if ttfb_ms < TTFB_GOOD:
        score += 3
    elif ttfb_ms < TTFB_OK:
        score += 2
    else:
        score += 0

    if size_bytes < SIZE_GOOD:
        score += 3
    elif size_bytes < SIZE_OK:
        score += 2
    else:
        score += 0

    if lazy_pct >= 80:
        score += 2
    elif lazy_pct >= 50:
        score += 1

    if has_compression:
        score += 2

    if score >= 9:
        return "A"
    elif score >= 6:
        return "B"
    elif score >= 3:
        return "C"
    return "D"


def check_lazy_loading(content: str) -> dict:
    """Check images for lazy loading attributes."""
    images = re.findall(r'<img\s[^>]*>', content, re.I)
    total = len(images)
    lazy = sum(1 for img in images if 'loading="lazy"' in img or "loading='lazy'" in img)
    not_lazy = total - lazy

    # List images without lazy loading
    missing = []
    for img in images:
        if 'loading="lazy"' not in img and "loading='lazy'" not in img:
            src_m = re.search(r'src=["\']([^"\']+)', img)
            if src_m:
                missing.append(src_m.group(1)[:200])

    return {
        "total_images": total,
        "lazy_loaded": lazy,
        "not_lazy_loaded": not_lazy,
        "lazy_pct": round(lazy / total * 100, 1) if total > 0 else 100.0,
        "missing_lazy": missing[:10],
    }


async def check_performance(url: str) -> dict:
    """Measure performance metrics for a single URL."""
    result = {
        "url": url,
        "ttfb_ms": 0,
        "size_bytes": 0,
        "total_images": 0,
        "lazy_loaded": 0,
        "not_lazy_loaded": 0,
        "lazy_pct": 100.0,
        "compression": None,
        "ssl_valid": False,
        "external_scripts": 0,
        "external_stylesheets": 0,
        "grade": "D",
        "error": None,
    }

    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "PetHubMaintenanceBot/1.0"},
            verify=True,
        ) as client:
            start = time.monotonic()
            resp = await client.get(url, timeout=30, follow_redirects=True)
            ttfb = (time.monotonic() - start) * 1000

            result["ttfb_ms"] = round(ttfb, 1)
            result["size_bytes"] = len(resp.content)
            result["compression"] = resp.headers.get("content-encoding", "none")
            result["ssl_valid"] = url.startswith("https://")

            html = resp.text
            lazy_info = check_lazy_loading(html)
            result.update({
                "total_images": lazy_info["total_images"],
                "lazy_loaded": lazy_info["lazy_loaded"],
                "not_lazy_loaded": lazy_info["not_lazy_loaded"],
                "lazy_pct": lazy_info["lazy_pct"],
            })

            # Count external scripts and stylesheets
            scripts = re.findall(r'<script\s[^>]*src=["\']([^"\']+)', html, re.I)
            result["external_scripts"] = len(scripts)

            stylesheets = re.findall(r'<link\s[^>]*rel=["\']stylesheet["\'][^>]*href=["\']([^"\']+)', html, re.I)
            result["external_stylesheets"] = len(stylesheets)

            has_compression = result["compression"] in ("gzip", "br", "deflate")
            result["grade"] = _grade_page(ttfb, result["size_bytes"], result["lazy_pct"], has_compression)

    except Exception as e:
        result["error"] = str(e)[:200]
        logger.error(f"Performance check failed for {url}: {e}")

    return result


async def scan_all_performance() -> dict:
    """Run performance checks for all published pages."""
    started = datetime.now(timezone.utc)
    results = []
    grades = {"A": 0, "B": 0, "C": 0, "D": 0}

    async with httpx.AsyncClient(verify=False) as client:
        urls = []
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
                        title = item.get("title", {}).get("rendered", "")
                        link = item.get("link", "")
                        if link:
                            urls.append({"url": link, "title": title, "id": item["id"]})
                    if len(data) < 50:
                        break
                    page += 1
                except Exception as e:
                    logger.error(f"Failed to fetch {endpoint}: {e}")
                    break

    # Check each URL with rate limiting
    for item in urls:
        perf = await check_performance(item["url"])
        perf["title"] = item["title"]
        perf["page_id"] = item["id"]
        results.append(perf)
        grades[perf["grade"]] = grades.get(perf["grade"], 0) + 1
        await asyncio.sleep(0.5)

    total = len(results)
    avg_ttfb = round(sum(r["ttfb_ms"] for r in results) / total, 1) if total else 0
    avg_size = round(sum(r["size_bytes"] for r in results) / total) if total else 0
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()

    return {
        "scanned_at": started.isoformat(),
        "elapsed_seconds": round(elapsed, 1),
        "total_pages": total,
        "avg_ttfb_ms": avg_ttfb,
        "avg_size_bytes": avg_size,
        "grades": grades,
        "results": results,
    }
