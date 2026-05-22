import re
import logging
from datetime import datetime, timezone

import httpx

from config import settings

logger = logging.getLogger("maintenance-agent.duplicates")

WP_AUTH = (settings.WP_USER, settings.WP_APP_PASSWORD)


def _strip_html(html: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'\s+', ' ', text)
    return text.strip().lower()


def _get_word_set(text: str) -> set:
    words = re.findall(r'\b[a-z]{3,}\b', text)
    return set(words)


def _jaccard_similarity(set_a: set, set_b: set) -> float:
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)


async def _fetch_all_content() -> list[dict]:
    items = []
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
                        items.append({
                            "id": item["id"],
                            "title": item.get("title", {}).get("rendered", ""),
                            "url": item.get("link", ""),
                            "content": item.get("content", {}).get("rendered", ""),
                            "excerpt": item.get("excerpt", {}).get("rendered", ""),
                            "date": item.get("date", ""),
                            "type": endpoint.rstrip("s"),
                        })
                    if len(data) < 50:
                        break
                    page += 1
                except Exception as e:
                    logger.error(f"Failed to fetch {endpoint} page {page}: {e}")
                    break
    return items


async def scan_for_duplicates() -> dict:
    """Scan all pages/posts for duplicate content using Jaccard similarity."""
    started = datetime.now(timezone.utc)
    items = await _fetch_all_content()

    # Precompute text and word sets
    processed = []
    for item in items:
        text = _strip_html(item["content"])
        words = _get_word_set(text)
        processed.append({
            **item,
            "plain_text": text,
            "word_set": words,
            "word_count": len(words),
        })

    duplicates = []
    title_duplicates = []

    # Compare all pairs
    for i in range(len(processed)):
        for j in range(i + 1, len(processed)):
            a, b = processed[i], processed[j]

            # Skip very short pages (< 20 unique words)
            if a["word_count"] < 20 or b["word_count"] < 20:
                continue

            similarity = _jaccard_similarity(a["word_set"], b["word_set"])
            if similarity > 0.60:
                duplicates.append({
                    "page_a": {"id": a["id"], "title": a["title"], "url": a["url"], "type": a["type"], "date": a["date"], "word_count": a["word_count"]},
                    "page_b": {"id": b["id"], "title": b["title"], "url": b["url"], "type": b["type"], "date": b["date"], "word_count": b["word_count"]},
                    "similarity": round(similarity * 100, 1),
                })

            # Check title similarity
            title_a = a["title"].strip().lower()
            title_b = b["title"].strip().lower()
            if title_a and title_b and (title_a == title_b or _jaccard_similarity(set(title_a.split()), set(title_b.split())) > 0.8):
                title_duplicates.append({
                    "page_a": {"id": a["id"], "title": a["title"], "url": a["url"]},
                    "page_b": {"id": b["id"], "title": b["title"], "url": b["url"]},
                    "type": "identical_title" if title_a == title_b else "similar_title",
                })

    duplicates.sort(key=lambda x: x["similarity"], reverse=True)
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()

    return {
        "scanned_at": started.isoformat(),
        "elapsed_seconds": round(elapsed, 1),
        "total_pages": len(items),
        "duplicate_pairs": len(duplicates),
        "title_duplicates": len(title_duplicates),
        "duplicates": duplicates,
        "title_duplicates_list": title_duplicates,
        "canonicals": suggest_canonical(duplicates),
    }


def suggest_canonical(duplicates: list) -> list:
    """For each duplicate pair, suggest which should be canonical."""
    suggestions = []
    for dup in duplicates:
        a, b = dup["page_a"], dup["page_b"]
        # Prefer older page, or page with more words
        canonical = a
        alternate = b
        if a["date"] and b["date"]:
            if a["date"] > b["date"]:
                canonical, alternate = b, a
        elif a.get("word_count", 0) < b.get("word_count", 0):
            canonical, alternate = b, a

        suggestions.append({
            "canonical": {"id": canonical["id"], "title": canonical["title"], "url": canonical["url"]},
            "alternate": {"id": alternate["id"], "title": alternate["title"], "url": alternate["url"]},
            "similarity": dup["similarity"],
            "reason": "Older/more authoritative page preferred as canonical",
        })
    return suggestions
