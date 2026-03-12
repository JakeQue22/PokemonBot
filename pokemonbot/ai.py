"""xAI (Grok) integration for AI-powered stock detection.

When traditional regex pattern matching fails to determine product
availability, the page content can be sent to xAI's Grok model for
intelligent analysis.  This acts as a fallback so that new or unusual
page layouts are still correctly interpreted.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# xAI API endpoint (OpenAI-compatible).
_XAI_API_URL = "https://api.x.ai/v1/chat/completions"

# Model to use for analysis.
_XAI_MODEL = "grok-3"

# Maximum characters of page body to send to the API to keep token
# usage reasonable.  ~12 000 chars ≈ ~3 000 tokens.
_MAX_BODY_CHARS = 12_000

# Request timeout in seconds for the xAI API call.
_API_TIMEOUT = 15.0

# System prompt that instructs the model to focus on product stock.
_SYSTEM_PROMPT = (
    "You are an expert e-commerce page analyser.  Given the HTML content "
    "of a product page, determine whether the product is currently available "
    "for purchase.\n\n"
    "Reply with EXACTLY one of these words on its own line:\n"
    "  IN_STOCK   – the product can be added to cart/basket and purchased\n"
    "  OUT_OF_STOCK – the product is sold out, unavailable, or cannot be purchased\n"
    "  UNKNOWN    – the page does not contain enough information to determine availability\n\n"
    "Then on the next line, provide a brief one-sentence reason."
)


async def analyse_page(api_key: str, html_body: str, url: str) -> str | None:
    """Ask xAI Grok to analyse a page and determine stock status.

    Parameters
    ----------
    api_key:
        xAI API key (Bearer token).
    html_body:
        Raw HTML of the product page.
    url:
        The page URL (included for context).

    Returns
    -------
    ``"in_stock"``, ``"out_of_stock"``, or ``None`` when the result is
    unknown or an error occurs.
    """
    if not api_key:
        return None

    # Truncate the body to limit token usage.
    truncated = html_body[:_MAX_BODY_CHARS]
    if len(html_body) > _MAX_BODY_CHARS:
        truncated += "\n... [truncated]"

    user_message = (
        f"URL: {url}\n\n"
        f"Page HTML:\n{truncated}"
    )

    payload: dict[str, Any] = {
        "model": _XAI_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.0,
        "max_tokens": 100,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        timeout = aiohttp.ClientTimeout(total=_API_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                _XAI_API_URL,
                json=payload,
                headers=headers,
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(
                        "xAI API returned HTTP %d: %s", resp.status, body[:200],
                    )
                    return None

                data = await resp.json()
    except Exception as exc:
        logger.warning("xAI API call failed: %s", exc)
        return None

    return _parse_response(data)


def _parse_response(data: dict[str, Any]) -> str | None:
    """Extract the stock verdict from an xAI chat completion response."""
    try:
        content: str = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("xAI API response missing expected fields")
        return None

    first_line = content.strip().splitlines()[0].strip().upper()

    if first_line == "IN_STOCK":
        logger.info("xAI analysis: IN_STOCK – %s", content.strip())
        return "in_stock"
    if first_line == "OUT_OF_STOCK":
        logger.info("xAI analysis: OUT_OF_STOCK – %s", content.strip())
        return "out_of_stock"

    logger.info("xAI analysis: UNKNOWN – %s", content.strip())
    return None
