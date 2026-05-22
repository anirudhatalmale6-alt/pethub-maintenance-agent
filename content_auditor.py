import re
import logging
from datetime import datetime, timezone

import httpx

from config import settings

logger = logging.getLogger("maintenance-agent.content")

WP_AUTH = (settings.WP_USER, settings.WP_APP_PASSWORD)

FRESHNESS_DAYS = 90


def _strip_html(html: str) -> str:
    return re.sub(r'<[^>]+>', ' ', html)


def _word_count(text: str) -> int:
    return len(text.split())


def _count_headings(html: str) -> dict:
    h1 = len(re.findall(r'<h1[\s>]', html, re.I))
    h2 = len(re.findall(r'<h2[\s>]', html, re.I))
    h3 = len(re.findall(r'<h3[\s>]', html, re.I))
    return {"h1": h1, "h2": h2, "h3": h3}


def _count_internal_links(html: str, site_url: str) -> int:
    from urllib.parse import urlparse
    host = urlparse(site_url).hostname
    links = re.findall(r'<a\s[^>]*href=["\']([^"\']+)', html, re.I)
    count = 0
    for link in links:
        try:
            parsed = urlparse(link)
            if parsed.hostname == host or (not parsed.hostname and link.startswith("/")):
                count += 1
        except Exception:
            pass
    return count


def _count_images(html: str) -> int:
    return len(re.findall(r'<img\s', html, re.I))


def _calculate_health_score(word_count: int, days_since_update: int, internal_links: int, images: int, headings: dict) -> float:
    """Health score 0-100 based on: words(30%), freshness(20%), links(20%), images(15%), headings(15%)."""
    # Word count score (0-30): 300+ words = full marks
    words_score = min(word_count / 300, 1.0) * 30

    # Freshness score (0-20): updated within 90 days = full marks
    if days_since_update <= FRESHNESS_DAYS:
        fresh_score = 20
    elif days_since_update <= 180:
        fresh_score = 10
    else:
        fresh_score = max(0, 20 - (days_since_update - 90) / 30)

    # Internal links (0-20): 3+ links = full marks
    links_score = min(internal_links / 3, 1.0) * 20

    # Images (0-15): 1+ image = full marks
    images_score = min(images, 1) * 15

    # Headings (0-15): at least 1 H2 = full marks
    headings_score = min(headings.get("h2", 0), 1) * 15

    return round(words_score + fresh_score + links_score + images_score + headings_score, 1)


async def audit_content() -> dict:
    """Audit content quality for all published pages/posts."""
    started = datetime.now(timezone.utc)
    results = []

    async with httpx.AsyncClient(verify=False) as client:
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
                        content_html = item.get("content", {}).get("rendered", "")
                        plain_text = _strip_html(content_html)
                        wc = _word_count(plain_text)
                        headings = _count_headings(content_html)
                        internal_links = _count_internal_links(content_html, settings.WP_URL)
                        images = _count_images(content_html)
                        modified = item.get("modified", item.get("date", ""))

                        days_since = 0
                        try:
                            mod_dt = datetime.fromisoformat(modified.replace("Z", "+00:00"))
                            if mod_dt.tzinfo is None:
                                mod_dt = mod_dt.replace(tzinfo=timezone.utc)
                            days_since = (datetime.now(timezone.utc) - mod_dt).days
                        except Exception:
                            days_since = 999

                        health = _calculate_health_score(wc, days_since, internal_links, images, headings)

                        results.append({
                            "id": item["id"],
                            "title": item.get("title", {}).get("rendered", ""),
                            "url": item.get("link", ""),
                            "type": endpoint.rstrip("s"),
                            "word_count": wc,
                            "last_modified": modified,
                            "days_since_update": days_since,
                            "internal_links": internal_links,
                            "images": images,
                            "headings": headings,
                            "health_score": health,
                        })
                    if len(data) < 50:
                        break
                    page += 1
                except Exception as e:
                    logger.error(f"Failed to fetch {endpoint}: {e}")
                    break

    results.sort(key=lambda x: x["health_score"])
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    avg_health = round(sum(r["health_score"] for r in results) / len(results), 1) if results else 0

    return {
        "scanned_at": started.isoformat(),
        "elapsed_seconds": round(elapsed, 1),
        "total_pages": len(results),
        "avg_health_score": avg_health,
        "results": results,
        "recommendations": get_recommendations(results),
    }


def get_recommendations(results: list = None) -> list:
    """Generate prioritized recommendations based on content audit."""
    if not results:
        return []

    recs = []
    for r in results:
        if r["word_count"] < 300:
            recs.append({
                "page": r["title"],
                "url": r["url"],
                "issue": "thin_content",
                "message": f"Thin content ({r['word_count']} words) - consider expanding to 300+ words",
                "priority": 1,
                "impact": "high",
            })

        if r["days_since_update"] > FRESHNESS_DAYS:
            recs.append({
                "page": r["title"],
                "url": r["url"],
                "issue": "stale_content",
                "message": f"Not updated in {r['days_since_update']} days - refresh recommended",
                "priority": 2,
                "impact": "medium",
            })

        if r["internal_links"] == 0:
            recs.append({
                "page": r["title"],
                "url": r["url"],
                "issue": "no_internal_links",
                "message": "No internal links - add links to improve SEO and navigation",
                "priority": 3,
                "impact": "medium",
            })

        if r["images"] == 0:
            recs.append({
                "page": r["title"],
                "url": r["url"],
                "issue": "no_images",
                "message": "No images - add images for better engagement",
                "priority": 4,
                "impact": "low",
            })

        if r["headings"].get("h2", 0) == 0:
            recs.append({
                "page": r["title"],
                "url": r["url"],
                "issue": "no_subheadings",
                "message": "No H2 subheadings - add structure for readability and SEO",
                "priority": 5,
                "impact": "low",
            })

    recs.sort(key=lambda x: x["priority"])
    return recs
