"""Wunderflats scraper.

WHY HTTP, NOT A BROWSER:
    Wunderflats is a Next.js/SPA site that embeds the *entire* search result set
    as inline JSON in the page HTML (a big <script type="application/json"> blob
    holding the app state). We fetch the public city search page with plain HTTP,
    extract that JSON, and read `pageData.listingResults.items`. This is:
      - far cheaper and faster than driving a browser,
      - more stable than parsing rendered DOM,
      - and it already includes GPS coordinates, so these listings need NO geocode.

    Wunderflats is a furnished-only marketplace, so `furnished` is always True and
    the advertised monthly price is the all-inclusive rent (treated as Warmmiete).

FRAGILE BITS (update here if the site changes):
    - SEARCH_URL / pagination is path-based: /en/furnished-apartments/<city>/<page>
    - The JSON lives in the first large <script type="application/json"> tag and
      the listings are at pageData.listingResults.items
    - Listing detail URL route (from their JS bundle): /en/furnished-apartment/<slug>/<_id>
      The <slug> is decorative — the <_id> is what resolves (a wrong slug 301-redirects
      to the canonical one), so we slugify the title for a clean, working link.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime, timedelta

from selectolax.parser import HTMLParser

from ..models import Listing
from .base import BaseScraper

logger = logging.getLogger("housing_agent")

BASE = "https://wunderflats.com"
# City search path. Wunderflats maps our config city -> its region slug.
CITY_SLUGS = {"munich": "munich", "münchen": "munich", "muenchen": "munich"}


def _slugify(text: str) -> str:
    """Best-effort slug from a title (matches Wunderflats' scheme closely enough;
    a mismatch just triggers a 301 to the canonical URL)."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "apartment"


class WunderflatsScraper(BaseScraper):
    name = "wunderflats"

    def _city_slug(self) -> str:
        city = self.config.search.city.strip().lower()
        return CITY_SLUGS.get(city, city)

    def _page_url(self, page: int) -> str:
        """Page 1 has no numeric suffix; later pages are /<n>. We also pass the
        price/room filters as query params to trim the candidate set server-side
        (central filters.py re-checks everything, so param drift is harmless)."""
        city = self._city_slug()
        path = f"/en/furnished-apartments/{city}" if page <= 1 else \
               f"/en/furnished-apartments/{city}/{page}"
        s = self.config.search
        params = {
            "maxPrice": int(s.max_price_eur * 100),   # Wunderflats prices are in cents
            "minRooms": s.min_rooms,
            "maxRooms": s.max_rooms,
        }
        # Availability window. Wunderflats uses `from`/`to` (ISO dates) to filter by
        # move-in/move-out. We query from the EARLIEST acceptable move-in so both the
        # preferred and fallback lease starts are covered in one search.
        move_from = s.earliest_move_in or s.move_in_date
        if move_from:
            params["from"] = move_from
            # Require availability through the configured end date (excludes listings
            # that end too early); fall back to from + min_lease_months.
            move_to = s.min_available_until or self._window_end(move_from, s.min_lease_months)
            if move_to:
                params["to"] = move_to
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{BASE}{path}?{query}"

    @staticmethod
    def _window_end(start_iso: str, months: int | None) -> str | None:
        """Compute an availability window end = start + min_lease_months (approx,
        30-day months are fine for a coarse availability filter)."""
        if not months:
            return None
        try:
            start = datetime.strptime(start_iso, "%Y-%m-%d").date()
        except ValueError:
            return None
        return (start + timedelta(days=30 * months)).isoformat()

    @staticmethod
    def _extract_state(html: str) -> dict:
        """Pull the inline app-state JSON out of the page."""
        tree = HTMLParser(html)
        for node in tree.css('script[type="application/json"]'):
            raw = node.text() or ""
            if len(raw) < 1000:  # skip tiny config blobs
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and "pageData" in data:
                return data
        return {}

    def _detail_url(self, item: dict) -> str:
        title = self._title(item)
        return f"{BASE}/en/furnished-apartment/{_slugify(title)}/{item.get('_id', '')}"

    @staticmethod
    def _title(item: dict) -> str:
        t = item.get("title")
        if isinstance(t, dict):
            return t.get("en") or t.get("de") or "Untitled listing"
        return t or "Untitled listing"

    def _to_listing(self, item: dict) -> Listing | None:
        listing_id = item.get("_id")
        if not listing_id:
            return None

        # Price is in EUR cents; Wunderflats furnished rent is all-inclusive (warm).
        price_cents = item.get("price")
        price_eur = round(price_cents / 100, 2) if isinstance(price_cents, (int, float)) else None

        # Coordinates are GeoJSON [lng, lat] — note the order.
        lat = lng = None
        addr = item.get("address") or {}
        coords = (addr.get("location") or {}).get("coordinates")
        if isinstance(coords, list) and len(coords) == 2:
            lng, lat = coords[0], coords[1]

        area_parts = [addr.get("street"), addr.get("city")]
        address_or_area = ", ".join(p for p in area_parts if p) or self.config.search.city.title()

        return Listing(
            source=self.name,
            url=self._detail_url(item),
            listing_id=str(listing_id),
            title=self._title(item),
            price_eur=price_eur,
            price_type="warm",         # Wunderflats advertises all-inclusive rent
            rooms=item.get("rooms"),
            furnished=True,            # furnished-only marketplace
            address_or_area=address_or_area,
            lat=lat,
            lng=lng,
            area_sqm=item.get("area"),
            extra={
                "beds": item.get("beds"),
                "accommodates": item.get("accommodates"),
                "labels": item.get("labels"),
                # Wunderflats flags student-only listings; we filter these out since
                # the search is for a working professional (see filters.py).
                "only_students": bool((item.get("restrictions") or {}).get("onlyStudentsAllowed")),
            },
        )

    def scrape(self) -> list[Listing]:
        listings: list[Listing] = []
        max_pages = max(1, self.source_cfg.max_pages)
        total: int | None = None

        for page in range(1, max_pages + 1):
            url = self._page_url(page)
            try:
                resp = self.get(url)
            except Exception as exc:  # network/HTTP error — stop paginating, keep what we have
                logger.warning("[wunderflats] page %d failed: %s", page, exc)
                break

            state = self._extract_state(resp.text)
            results = (state.get("pageData") or {}).get("listingResults") or {}
            items = results.get("items") or []
            if total is None:
                total = results.get("total")
                logger.info("[wunderflats] %s total listings match the city query", total)

            if not items:
                logger.info("[wunderflats] no items on page %d — stopping", page)
                break

            for item in items:
                listing = self._to_listing(item)
                if listing:
                    listings.append(listing)

            # Stop early if we've paged past the result count.
            if total is not None and page * results.get("itemsPerPage", 30) >= total:
                break

        logger.info("[wunderflats] collected %d raw listings", len(listings))
        return listings
