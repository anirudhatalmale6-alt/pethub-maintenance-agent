"""OpenAI GPT integration for the Maintenance Agent.

Uses httpx to call OpenAI API directly (no openai SDK dependency).
Provides AI-powered content quality scoring and duplicate intent detection.
"""

import json
import logging
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger("maintenance.ai")

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
TIMEOUT = 15.0

SYSTEM_PROMPT = (
    "You are a content quality analyst for Pet Hub Online (pethubonline.com), "
    "a UK-based pet supplies affiliate website. You evaluate content for readability, "
    "SEO effectiveness, structure, and user value. You are precise and analytical."
)


async def _call_openai(
    messages: list[dict],
    model: str = "gpt-4o-mini",
    temperature: float = 0.3,
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
        logger.error("OpenAI API HTTP %d: %s", exc.response.status_code, exc.response.text[:300])
        return None
    except Exception as exc:
        logger.error("OpenAI API unexpected error: %s", exc)
        return None


async def ai_score_content_quality(
    title: str,
    content_snippet: str,
    word_count: int,
) -> Optional[dict]:
    """Score content quality and identify issues.

    Args:
        title: Page title.
        content_snippet: First ~500 chars of page content.
        word_count: Total word count of the page.

    Returns:
        Dict with "score" (0-100), "issues" (list of strings),
        and "suggestions" (list of strings), or None on failure.
    """
    user_prompt = (
        f"Page title: {title}\n"
        f"Word count: {word_count}\n"
        f"Content snippet: {content_snippet}\n\n"
        "Evaluate this content for a pet supplies affiliate page. Score it 0-100 based on:\n"
        "- Readability and clarity (25 points)\n"
        "- Keyword usage and SEO potential (25 points)\n"
        "- Content structure and formatting signals (25 points)\n"
        "- User value and purchase intent support (25 points)\n\n"
        "Return a JSON object with:\n"
        '- "score": integer 0-100\n'
        '- "issues": array of specific problems found (max 5)\n'
        '- "suggestions": array of actionable improvements (max 5)\n\n'
        "Return ONLY the JSON object, no markdown formatting."
    )

    result = await _call_openai(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        model="gpt-4o",
        temperature=0.3,
        max_tokens=600,
    )

    if result is None:
        return None

    try:
        cleaned = result.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

        parsed = json.loads(cleaned)
        if isinstance(parsed, dict) and "score" in parsed:
            return {
                "score": int(parsed.get("score", 0)),
                "issues": list(parsed.get("issues", [])),
                "suggestions": list(parsed.get("suggestions", [])),
            }
        logger.warning("OpenAI returned unexpected structure for content quality: %s", parsed.keys() if isinstance(parsed, dict) else type(parsed))
        return None
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("Failed to parse content quality JSON: %s", exc)
        return None


async def ai_detect_duplicate_intent(
    title_a: str,
    snippet_a: str,
    title_b: str,
    snippet_b: str,
) -> Optional[dict]:
    """Check if two pages serve the same user intent (semantic duplicate detection).

    Args:
        title_a: Title of the first page.
        snippet_a: Content snippet of the first page.
        title_b: Title of the second page.
        snippet_b: Content snippet of the second page.

    Returns:
        Dict with "is_duplicate" (bool), "similarity" (float 0-1),
        and "recommendation" (str), or None on failure.
    """
    user_prompt = (
        "Compare these two pages for semantic intent overlap:\n\n"
        f"Page A title: {title_a}\n"
        f"Page A content: {snippet_a}\n\n"
        f"Page B title: {title_b}\n"
        f"Page B content: {snippet_b}\n\n"
        "Assess whether these pages serve the same user intent (not just word overlap, "
        "but whether a user searching for one would be equally satisfied by the other).\n\n"
        "Return a JSON object with:\n"
        '- "is_duplicate": true if they substantially overlap in intent, false otherwise\n'
        '- "similarity": float 0.0 to 1.0 indicating intent overlap\n'
        '- "recommendation": one of "merge", "differentiate", "keep_both" with a brief reason\n\n'
        "Return ONLY the JSON object, no markdown formatting."
    )

    result = await _call_openai(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        model="gpt-4o",
        temperature=0.2,
        max_tokens=300,
    )

    if result is None:
        return None

    try:
        cleaned = result.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

        parsed = json.loads(cleaned)
        if isinstance(parsed, dict) and "is_duplicate" in parsed:
            return {
                "is_duplicate": bool(parsed.get("is_duplicate", False)),
                "similarity": float(parsed.get("similarity", 0.0)),
                "recommendation": str(parsed.get("recommendation", "keep_both")),
            }
        logger.warning("OpenAI returned unexpected structure for duplicate detection: %s", parsed.keys() if isinstance(parsed, dict) else type(parsed))
        return None
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("Failed to parse duplicate detection JSON: %s", exc)
        return None
