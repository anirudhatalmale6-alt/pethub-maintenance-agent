import re
import logging
import asyncio
from datetime import datetime, timezone

import httpx

from config import settings

logger = logging.getLogger("maintenance-agent.metadata")

WP_AUTH = (settings.WP_USER, settings.WP_APP_PASSWORD)


def _extract_meta(html: str, url: str) -> dict:
    """Extract meta tags from rendered HTML."""
    meta = {
        "title": "",
        "title_length": 0,
        "description": "",
        "description_length": 0,
        "canonical": "",
        "canonical_matches": False,
        "og_title": "",
        "og_description": "",
        "og_image": "",
        "issues": [],
    }

    # Title tag
    m = re.search(r'<title[^>]*>(.*?)</title>', html, re.S | re.I)
    if m:
        meta["title"] = m.group(1).strip()
        meta["title_length"] = len(meta["title"])
    else:
        meta["issues"].append("missing_title")

    # Meta description
    m = re.search(r'<meta\s[^>]*name=["\']description["\'][^>]*content=["\']([^"\']*)', html, re.I)
    if not m:
        m = re.search(r'<meta\s[^>]*content=["\']([^"\']*)["\'][^>]*name=["\']description["\']', html, re.I)
    if m:
        meta["description"] = m.group(1).strip()
        meta["description_length"] = len(meta["description"])
    else:
        meta["issues"].append("missing_description")

    # Canonical
    m = re.search(r'<link\s[^>]*rel=["\']canonical["\'][^>]*href=["\']([^"\']+)', html, re.I)
    if m:
        meta["canonical"] = m.group(1).strip()
        meta["canonical_matches"] = meta["canonical"].rstrip("/") == url.rstrip("/")
    else:
        meta["issues"].append("missing_canonical")

    # Open Graph
    for tag, key in [("og:title", "og_title"), ("og:description", "og_description"), ("og:image", "og_image")]:
        m = re.search(rf'<meta\s[^>]*property=["\']{ re.escape(tag) }["\'][^>]*content=["\']([^"\']*)', html, re.I)
        if m:
            meta[key] = m.group(1).strip()
        else:
            meta["issues"].append(f"missing_{key}")

    # Length checks
    if meta["title_length"] > 0:
        if meta["title_length"] < 30:
            meta["issues"].append("title_too_short")
        elif meta["title_length"] > 70:
            meta["issues"].append("title_too_long")

    if meta["description_length"] > 0:
        if meta["description_length"] < 70:
            meta["issues"].append("description_too_short")
        elif meta["description_length"] > 170:
            meta["issues"].append("description_too_long")

    if meta["canonical"] and not meta["canonical_matches"]:
        meta["issues"].append("canonical_mismatch")

    return meta


async def audit_all_metadata() -> dict:
    """Audit meta tags for all published pages/posts by fetching live HTML."""
    started = datetime.now(timezone.utc)
    results = []
    all_titles = []
    all_descriptions = []
    total_issues = 0

    # First get the page list from API
    pages_list = []
    async with httpx.AsyncClient(verify=False) as api_client:
        for endpoint in ["posts", "pages"]:
            page = 1
            while True:
                try:
                    resp = await api_client.get(
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
                        pages_list.append({
                            "id": item["id"],
                            "title": item.get("title", {}).get("rendered", ""),
                            "url": item.get("link", ""),
                            "type": endpoint.rstrip("s"),
                        })
                    if len(data) < 50:
                        break
                    page += 1
                except Exception as e:
                    logger.error(f"Failed to fetch {endpoint}: {e}")
                    break

    # Fetch live HTML for each page
    async with httpx.AsyncClient(
        headers={"User-Agent": "PetHubMaintenanceBot/1.0"},
        verify=False,
    ) as client:
        for pg in pages_list:
            try:
                resp = await client.get(pg["url"], timeout=30, follow_redirects=True)
                if resp.status_code == 200:
                    meta = _extract_meta(resp.text, pg["url"])
                    meta["page_id"] = pg["id"]
                    meta["page_title"] = pg["title"]
                    meta["page_url"] = pg["url"]
                    meta["page_type"] = pg["type"]
                    results.append(meta)
                    total_issues += len(meta["issues"])
                    all_titles.append(meta["title"])
                    all_descriptions.append(meta["description"])
            except Exception as e:
                logger.error(f"Failed to fetch HTML for {pg['url']}: {e}")
                results.append({
                    "page_id": pg["id"],
                    "page_title": pg["title"],
                    "page_url": pg["url"],
                    "page_type": pg["type"],
                    "issues": ["fetch_failed"],
                    "error": str(e)[:200],
                })
                total_issues += 1
            await asyncio.sleep(0.3)

    # Check for duplicate titles/descriptions across pages
    duplicate_titles = []
    duplicate_descriptions = []
    seen_titles = {}
    seen_descs = {}

    for r in results:
        t = r.get("title", "").strip().lower()
        d = r.get("description", "").strip().lower()
        if t and t in seen_titles:
            duplicate_titles.append({"title": r.get("title"), "pages": [seen_titles[t], r.get("page_url")]})
        elif t:
            seen_titles[t] = r.get("page_url")

        if d and len(d) > 20 and d in seen_descs:
            duplicate_descriptions.append({"description": r.get("description", "")[:80], "pages": [seen_descs[d], r.get("page_url")]})
        elif d and len(d) > 20:
            seen_descs[d] = r.get("page_url")

    total_pages = len(results)
    pages_with_issues = sum(1 for r in results if r.get("issues"))
    consistency_score = round((1 - pages_with_issues / total_pages) * 100, 1) if total_pages else 0
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()

    return {
        "scanned_at": started.isoformat(),
        "elapsed_seconds": round(elapsed, 1),
        "total_pages": total_pages,
        "pages_with_issues": pages_with_issues,
        "total_issues": total_issues,
        "consistency_score": consistency_score,
        "duplicate_titles": duplicate_titles,
        "duplicate_descriptions": duplicate_descriptions,
        "results": results,
    }
