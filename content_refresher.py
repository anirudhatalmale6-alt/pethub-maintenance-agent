"""AI-powered content freshness automation for the Maintenance Agent.

Identifies stale content that needs refreshing and uses AI to suggest
specific update actions and generate new content snippets.

Uses httpx to call OpenAI API directly (no openai SDK dependency).
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger("maintenance.refresher")

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
TIMEOUT = 15.0

SYSTEM_PROMPT = (
    "You are a content freshness specialist for Pet Hub Online (pethubonline.com), "
    "a UK-based pet supplies affiliate website. You identify outdated content and "
    "suggest timely updates to keep pages relevant, accurate, and ranking well. "
    "You write in British English."
)


async def _call_openai(
    messages: list[dict],
    model: str = "gpt-4o-mini",
    temperature: float = 0.4,
    max_tokens: int = 500,
) -> Optional[str]:
    """Low-level helper to call the OpenAI chat completions endpoint."""
    headers = {
        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(OPENAI_API_URL, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
    except httpx.TimeoutException:
        logger.error("OpenAI API timeout after %.0fs", TIMEOUT)
        return None
    except httpx.HTTPStatusError as exc:
        logger.error(
            "OpenAI API HTTP %d: %s",
            exc.response.status_code,
            exc.response.text[:300],
        )
        return None
    except Exception as exc:
        logger.error("OpenAI API unexpected error: %s", exc)
        return None


def _clean_json(raw: str) -> str:
    """Strip markdown code fences from an AI response if present."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
    return cleaned


def _parse_date(date_str: str) -> Optional[datetime]:
    """Parse a date string into a timezone-aware datetime.

    Supports ISO 8601 formats and common date formats.
    """
    formats = [
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            continue
    return None


async def identify_stale_content(
    pages: list[dict],
    stale_days: int = 60,
) -> list[dict]:
    """Identify pages that need content refresh based on age.

    Args:
        pages: List of page dicts with keys: id, title, url, modified_date, word_count.
            modified_date can be an ISO 8601 string or a datetime object.
        stale_days: Number of days after which a page is considered stale (default 60).

    Returns:
        List of stale page dicts sorted by staleness (oldest first), each with:
        - id: page ID
        - title: page title
        - url: page URL
        - days_old: number of days since last modification
        - priority: "high", "medium", or "low"
        - reason: explanation of why the page needs refreshing
    """
    now = datetime.now(timezone.utc)
    stale_pages = []

    for page in pages:
        page_id = page.get("id", 0)
        title = page.get("title", "Untitled")
        url = page.get("url", "")
        word_count = page.get("word_count", 0)
        modified = page.get("modified_date")

        # Parse the modification date
        if isinstance(modified, str):
            mod_dt = _parse_date(modified)
        elif isinstance(modified, datetime):
            mod_dt = modified if modified.tzinfo else modified.replace(tzinfo=timezone.utc)
        else:
            # If no date available, treat as very stale
            mod_dt = None

        if mod_dt is None:
            days_old = 999
            reason = "No modification date available; content age unknown."
        else:
            delta = now - mod_dt
            days_old = delta.days

        if days_old < stale_days:
            continue

        # Determine priority based on staleness and content length
        if days_old > 180:
            priority = "high"
            reason = (
                f"Last updated {days_old} days ago (over 6 months). "
                "Content may contain outdated information, broken links, or "
                "superseded product recommendations."
            )
        elif days_old > 120:
            priority = "high" if word_count and word_count < 500 else "medium"
            reason = (
                f"Last updated {days_old} days ago (over 4 months). "
                "May need seasonal updates or new product additions."
            )
        else:
            priority = "medium" if word_count and word_count < 500 else "low"
            reason = (
                f"Last updated {days_old} days ago (over {stale_days} days). "
                "Consider reviewing for freshness and accuracy."
            )

        # Short, thin content is higher priority for refresh
        if word_count and word_count < 300:
            priority = "high"
            reason += " Content is also thin (under 300 words)."

        stale_pages.append({
            "id": page_id,
            "title": title,
            "url": url,
            "days_old": days_old,
            "priority": priority,
            "reason": reason,
        })

    # Sort by days_old descending (oldest first)
    stale_pages.sort(key=lambda p: p["days_old"], reverse=True)

    logger.info(
        "Identified %d stale pages out of %d total (threshold: %d days)",
        len(stale_pages),
        len(pages),
        stale_days,
    )

    return stale_pages


async def ai_suggest_refresh(
    title: str,
    content_snippet: str,
    days_since_update: int,
) -> Optional[dict]:
    """Use AI to suggest specific refresh actions for stale content.

    Args:
        title: Page title.
        content_snippet: First ~500 chars of page content.
        days_since_update: Number of days since the page was last modified.

    Returns:
        Dict with:
        - refresh_actions: list of specific actions to take
        - estimated_effort: "low", "medium", or "high"
        - priority_reason: explanation of why this refresh matters
        Or None on failure.
    """
    if not title:
        logger.warning("Missing title for refresh suggestion")
        return None

    user_prompt = (
        f"Page title: {title}\n"
        f"Days since last update: {days_since_update}\n"
        f"Content snippet: {content_snippet[:500] if content_snippet else '(no content available)'}\n\n"
        "This page on a UK pet supplies affiliate site needs refreshing. "
        "Suggest specific actions to update it.\n\n"
        "Consider:\n"
        "- Are product recommendations likely outdated?\n"
        "- Are there seasonal or trending topics to add?\n"
        "- Could pricing or availability info be stale?\n"
        "- Are there new products or brands to mention?\n"
        "- Would updated statistics or research improve the content?\n\n"
        "Return a JSON object with:\n"
        '- "refresh_actions": array of 3-5 specific actionable steps\n'
        '- "estimated_effort": "low" (quick text edits), "medium" (new sections), '
        'or "high" (major rewrite)\n'
        '- "priority_reason": one sentence explaining why this refresh is important '
        "for SEO and user value\n\n"
        "Return ONLY the JSON object, no markdown formatting."
    )

    result = await _call_openai(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        model="gpt-4o-mini",
        temperature=0.4,
        max_tokens=500,
    )

    if result is None:
        logger.error("Failed to get refresh suggestions from OpenAI")
        return None

    try:
        parsed = json.loads(_clean_json(result))
        if not isinstance(parsed, dict):
            logger.warning("OpenAI returned non-dict for refresh suggestions")
            return None

        return {
            "refresh_actions": [
                str(a) for a in parsed.get("refresh_actions", [])
            ],
            "estimated_effort": str(
                parsed.get("estimated_effort", "medium")
            ).lower(),
            "priority_reason": str(
                parsed.get("priority_reason", "Content freshness improves rankings.")
            ),
        }
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.error("Failed to parse refresh suggestions JSON: %s", exc)
        return None


async def generate_refresh_snippet(
    title: str,
    existing_content: str,
    topic_to_add: str,
) -> Optional[str]:
    """Use AI to generate a new paragraph to add to existing content.

    Generates a paragraph about the specified topic that matches the tone
    and style of the existing content.

    Args:
        title: Page title.
        existing_content: First ~500 chars of existing page content (for tone matching).
        topic_to_add: The specific topic or section to write about.

    Returns:
        Generated paragraph text, or None on failure.
    """
    if not title or not topic_to_add:
        logger.warning("Missing title or topic_to_add for snippet generation")
        return None

    user_prompt = (
        f"Page title: {title}\n"
        f"Existing content sample: {existing_content[:500] if existing_content else '(no existing content)'}\n\n"
        f"Write a new paragraph about: {topic_to_add}\n\n"
        "Requirements:\n"
        "- Match the tone and style of the existing content\n"
        "- Write in British English\n"
        "- Keep it informative and useful for pet owners\n"
        "- Include relevant keywords naturally\n"
        "- 80-150 words\n"
        "- Do not repeat information already in the existing content\n"
        "- Make it suitable for a UK pet supplies affiliate site\n\n"
        "Return ONLY the paragraph text, no headings, labels, or formatting."
    )

    result = await _call_openai(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        model="gpt-4o-mini",
        temperature=0.5,
        max_tokens=300,
    )

    if result:
        # Clean up any surrounding quotes or formatting
        result = result.strip('"\'')

    return result
