"""Enrich a live listing with its detail-page description + image URLs, so the
ranker can score on real photos/text instead of median-imputing those features.

Used only by the daily agent in `digest.mode: ranked`. Reuses the snapshot's
per-site extraction. Best-effort: if the detail page is blocked/unavailable, we
return {} and the scorer imputes aesthetic/description as before.
"""
from __future__ import annotations

import httpx
from selectolax.parser import HTMLParser

from .snapshot import (BLOCK_MARKERS, HEADERS, SITE_IMAGE_HOOKS, _dedupe, _jsonld,
                       _ld_field, _ld_images, _meta, _og_images)

MAX_IMAGES = 12


def enrich_listing(url: str, site: str, timeout: float = 25.0) -> dict:
    """Return {'description': str, 'image_urls': [...]} from the listing detail page,
    or {} if it can't be fetched/parsed."""
    try:
        r = httpx.get(url, headers=HEADERS, follow_redirects=True, timeout=timeout)
        if r.status_code != 200:
            return {}
        html = r.text
    except Exception:
        return {}
    low = html.lower()
    if len(html) < 15000 and any(m in low for m in BLOCK_MARKERS):
        return {}   # bot-challenge interstitial

    tree = HTMLParser(html)
    ld = _jsonld(tree)
    desc = (_ld_field(ld, "description") or _meta(tree, "og:description", "description") or "")[:4000]
    imgs = _ld_images(ld) + _og_images(tree)
    imgs += SITE_IMAGE_HOOKS.get(site, lambda t, h: [])(tree, html)
    return {"description": desc, "image_urls": _dedupe(imgs)[:MAX_IMAGES]}
