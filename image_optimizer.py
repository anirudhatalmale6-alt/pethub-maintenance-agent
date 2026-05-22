"""Image optimization analysis for the Maintenance Agent.

Analyses images on pages for optimization opportunities: oversized files,
missing alt text, missing lazy loading attributes, and missing dimensions.
Uses httpx for HTTP HEAD requests to check image file sizes.
"""

import logging
import re
from typing import Optional
from urllib.parse import urljoin

import httpx

logger = logging.getLogger("maintenance.images")

# Images larger than this threshold (in KB) are flagged for optimization
SIZE_THRESHOLD_KB = 200

# Expected compression savings for oversized images (40-60%, use midpoint)
ESTIMATED_SAVINGS_RATIO = 0.50

# Timeout for HEAD requests to check image sizes
HEAD_TIMEOUT = 10.0

# Max concurrent image checks
MAX_CONCURRENT = 10


def _extract_images(html: str, page_url: str) -> list[dict]:
    """Extract all <img> tags from HTML with their attributes.

    Args:
        html: Raw HTML content.
        page_url: Base URL for resolving relative image paths.

    Returns:
        List of dicts with keys: src, alt, has_width, has_height, has_lazy.
    """
    images = []
    # Match <img> tags and capture their attributes
    img_pattern = re.compile(r"<img\s([^>]+)/?>", re.I | re.S)

    for match in img_pattern.finditer(html):
        attrs_str = match.group(1)

        # Extract src
        src_match = re.search(r'src=["\']([^"\']+)["\']', attrs_str, re.I)
        if not src_match:
            continue
        src = src_match.group(1).strip()

        # Skip data URIs and SVGs
        if src.startswith("data:") or src.endswith(".svg"):
            continue

        # Resolve relative URLs
        full_src = urljoin(page_url, src)

        # Extract alt text
        alt_match = re.search(r'alt=["\']([^"\']*)["\']', attrs_str, re.I)
        alt = alt_match.group(1).strip() if alt_match else ""

        # Check for width attribute
        has_width = bool(re.search(r'\bwidth\s*=', attrs_str, re.I))

        # Check for height attribute
        has_height = bool(re.search(r'\bheight\s*=', attrs_str, re.I))

        # Check for lazy loading
        has_lazy = bool(re.search(r'loading\s*=\s*["\']lazy["\']', attrs_str, re.I))

        images.append({
            "src": full_src,
            "alt": alt,
            "has_width": has_width,
            "has_height": has_height,
            "has_lazy": has_lazy,
        })

    return images


async def _get_image_size(client: httpx.AsyncClient, url: str) -> Optional[float]:
    """Get image file size in KB via HEAD request.

    Args:
        client: httpx.AsyncClient instance.
        url: Image URL.

    Returns:
        File size in KB, or None if the size could not be determined.
    """
    try:
        resp = await client.head(url, follow_redirects=True)
        if resp.status_code == 200:
            content_length = resp.headers.get("content-length")
            if content_length:
                return int(content_length) / 1024.0
        # Some servers don't support HEAD; try GET with range
        if resp.status_code in (405, 403):
            resp = await client.get(
                url,
                headers={"Range": "bytes=0-0"},
                follow_redirects=True,
            )
            content_range = resp.headers.get("content-range", "")
            # Format: bytes 0-0/total_size
            if "/" in content_range:
                total = content_range.split("/")[-1]
                if total.isdigit():
                    return int(total) / 1024.0
        return None
    except Exception as exc:
        logger.debug("Could not get size for %s: %s", url, exc)
        return None


async def analyze_page_images(page_url: str, page_content_html: str) -> dict:
    """Analyse all images on a page for optimization opportunities.

    Args:
        page_url: URL of the page being analysed.
        page_content_html: Raw HTML content of the page.

    Returns:
        Dict with:
        - total_images: total number of images found
        - oversized: list of dicts with url and size_kb for images over threshold
        - missing_alt: count of images without alt text
        - missing_lazy: count of images without lazy loading
        - missing_dimensions: count of images without width/height attributes
        - optimization_potential_kb: estimated total KB savings
    """
    result = {
        "total_images": 0,
        "oversized": [],
        "missing_alt": 0,
        "missing_lazy": 0,
        "missing_dimensions": 0,
        "optimization_potential_kb": 0,
    }

    if not page_content_html:
        logger.warning("No HTML content provided for image analysis")
        return result

    images = _extract_images(page_content_html, page_url)
    result["total_images"] = len(images)

    if not images:
        return result

    # Count missing attributes
    result["missing_alt"] = sum(1 for img in images if not img["alt"])
    result["missing_lazy"] = sum(1 for img in images if not img["has_lazy"])
    result["missing_dimensions"] = sum(
        1 for img in images if not img["has_width"] or not img["has_height"]
    )

    # Check file sizes via HEAD requests
    unique_srcs = list({img["src"] for img in images})
    oversized = []

    async with httpx.AsyncClient(timeout=HEAD_TIMEOUT) as client:
        # Process in batches to avoid overwhelming the server
        for i in range(0, len(unique_srcs), MAX_CONCURRENT):
            batch = unique_srcs[i : i + MAX_CONCURRENT]
            for url in batch:
                size_kb = await _get_image_size(client, url)
                if size_kb is not None and size_kb > SIZE_THRESHOLD_KB:
                    oversized.append({
                        "url": url,
                        "size_kb": int(size_kb),
                    })

    result["oversized"] = sorted(oversized, key=lambda x: x["size_kb"], reverse=True)

    # Estimate potential savings
    total_oversized_kb = sum(img["size_kb"] for img in oversized)
    result["optimization_potential_kb"] = int(total_oversized_kb * ESTIMATED_SAVINGS_RATIO)

    logger.info(
        "Image analysis for %s: %d images, %d oversized, %d missing alt, "
        "%d missing lazy, ~%d KB savings potential",
        page_url,
        result["total_images"],
        len(result["oversized"]),
        result["missing_alt"],
        result["missing_lazy"],
        result["optimization_potential_kb"],
    )

    return result


async def check_image_sizes(image_urls: list[str]) -> list[dict]:
    """Check sizes of a list of image URLs.

    Args:
        image_urls: List of image URLs to check.

    Returns:
        List of dicts with:
        - url: the image URL
        - size_kb: file size in KB (0.0 if unknown)
        - needs_optimization: True if over the size threshold
    """
    results = []

    if not image_urls:
        return results

    async with httpx.AsyncClient(timeout=HEAD_TIMEOUT) as client:
        for i in range(0, len(image_urls), MAX_CONCURRENT):
            batch = image_urls[i : i + MAX_CONCURRENT]
            for url in batch:
                size_kb = await _get_image_size(client, url)
                results.append({
                    "url": url,
                    "size_kb": round(size_kb, 1) if size_kb is not None else 0.0,
                    "needs_optimization": (
                        size_kb is not None and size_kb > SIZE_THRESHOLD_KB
                    ),
                })

    return results


def estimate_savings(images: list[dict]) -> dict:
    """Estimate total potential savings from image optimization.

    Images over 200KB can typically be reduced by 40-60% through compression,
    format conversion (WebP/AVIF), and resizing.

    Args:
        images: List of dicts with at least a "size_kb" key (float/int).
            Optionally a "needs_optimization" bool key.

    Returns:
        Dict with:
        - total_current_kb: total size of all provided images
        - estimated_savings_kb: estimated KB that could be saved
        - images_to_optimize: count of images that need optimization
    """
    total_current_kb = 0.0
    estimated_savings_kb = 0.0
    images_to_optimize = 0

    for img in images:
        size_kb = float(img.get("size_kb", 0))
        total_current_kb += size_kb

        needs_opt = img.get("needs_optimization")
        if needs_opt is None:
            needs_opt = size_kb > SIZE_THRESHOLD_KB

        if needs_opt:
            images_to_optimize += 1
            estimated_savings_kb += size_kb * ESTIMATED_SAVINGS_RATIO

    return {
        "total_current_kb": round(total_current_kb, 1),
        "estimated_savings_kb": round(estimated_savings_kb, 1),
        "images_to_optimize": images_to_optimize,
    }
