"""HousingAnywhere scraper.

WHY DOM PARSING (not their API, not embedded JSON):
    - Their internal listings API (api.housinganywhere.com) requires an auth token
      (returns 401 anonymously).
    - The SSR state (window.__PRELOADED_STATE__) is not pure JSON.
    - BUT the search page renders listing cards server-side with stable
      `data-test-locator` attributes, so we parse those directly over plain HTTP.
      No browser required.

IMPORTANT — bedrooms vs Zimmer:
    HousingAnywhere counts BEDROOMS ("Studio", "1 bedroom", "2 bedrooms"), whereas
    German listings count Zimmer. We map to Zimmer for consistent filtering:
        Studio      -> 1 Zimmer
        1 bedroom   -> 2 Zimmer  (1 BR + living room)
        N bedrooms  -> N+1 Zimmer
    So a "1 bedroom" flat = 2 Zimmer, which is exactly the target.

    HousingAnywhere is a furnished / mid-term marketplace, so we mark furnished=True.

FRAGILE BITS (update here if the site changes):
    - SEARCH_URL and page param
    - The data-test-locator names below (Title/Price/AttributesSize/etc.)
    - Listings lack coordinates in the card, so they are geocoded downstream
      (commute.py / geocode.py) from the address text.
"""
from __future__ import annotations

import logging
import re

from selectolax.parser import HTMLParser, Node

from ..models import Listing
from .base import BaseScraper

logger = logging.getLogger("housing_agent")

BASE = "https://housinganywhere.com"
# HousingAnywhere titles its city in the search path (e.g. /s/Munich--Germany).
# Alias common German spellings; otherwise the configured city is title-cased.
CITY_ALIASES = {"münchen": "Munich", "muenchen": "Munich"}

_PRICE_RE = re.compile(r"([\d.,]+)")
_ID_RE = re.compile(r"/room/([a-z0-9]+)/", re.I)


def _parse_price(text: str) -> float | None:
    m = _PRICE_RE.search(text.replace("\xa0", " "))
    if not m:
        return None
    num = m.group(1).replace(".", "").replace(",", ".")
    try:
        return float(num)
    except ValueError:
        return None


def _places_to_zimmer(text: str) -> float | None:
    """Map HousingAnywhere bedroom text to German Zimmer count."""
    t = text.strip().lower()
    if "studio" in t:
        return 1
    m = re.search(r"(\d+)\s*bedroom", t)
    if m:
        return int(m.group(1)) + 1   # bedrooms + living room
    return None


class HousingAnywhereScraper(BaseScraper):
    name = "housinganywhere"

    def _city(self) -> str:
        city = self.config.search.city.strip().lower()
        return CITY_ALIASES.get(city, self.config.search.city.title())

    def _page_url(self, page: int) -> str:
        # e.g. https://housinganywhere.com/s/Munich--Germany?page=2
        # NOTE: adding price/currency query params makes HA return a page WITHOUT
        # server-rendered ListingCards (0 results to parse), so we intentionally
        # request the plain search page and let filters.py enforce the price cap.
        country = self.config.search.country.strip().replace(" ", "-")
        base = f"{BASE}/s/{self._city()}--{country}"
        return base if page <= 1 else f"{base}?page={page}"

    @staticmethod
    def _loc(card: Node, locator: str) -> str:
        node = card.css_first(f'[data-test-locator="{locator}"]')
        return (node.text() or "").strip() if node else ""

    @staticmethod
    def _listing_href(card: Node) -> str:
        """The /room/<id>/... link WRAPS the card, so it's an ancestor, not a
        descendant. Walk up parents to find it; fall back to any descendant link."""
        node: Node | None = card
        for _ in range(6):
            node = node.parent if node else None
            if node is None:
                break
            if node.tag == "a":
                h = node.attributes.get("href", "") or ""
                if "/room/" in h:
                    return h
        for a in card.css("a[href]"):
            h = a.attributes.get("href", "") or ""
            if "/room/" in h:
                return h
        return ""

    def _card_to_listing(self, card: Node) -> Listing | None:
        href = self._listing_href(card)
        if href.startswith("/"):
            href = BASE + href
        id_match = _ID_RE.search(href)
        if not id_match:
            return None
        listing_id = id_match.group(1)

        title = self._loc(card, "ListingCard/Title") or "HousingAnywhere listing"
        price = _parse_price(self._loc(card, "ListingCard/Price"))
        size = _parse_price(self._loc(card, "ListingCard/AttributesSize"))
        price_label = self._loc(card, "ListingCard/PriceLabel")
        availability = self._loc(card, "ListingCard/Availability")

        # Titles read "<type> in <street>, <city>". Skip shared/WG rooms — the user
        # wants a self-contained furnished 1-bedroom apartment, not a room in a
        # shared flat.
        prop_type = title.split(" in ", 1)[0].strip().lower() if " in " in title else ""
        if prop_type in ("private room", "room", "shared room"):
            return None

        # Rooms in German Zimmer: prefer the explicit "N bedroom(s)" attribute; for
        # a studio there's no bedroom attribute, so derive it from the title type.
        rooms = _places_to_zimmer(self._loc(card, "ListingCard/AttributesPlaces"))
        if rooms is None and "studio" in prop_type:
            rooms = 1

        area = title.split(" in ", 1)[1] if " in " in title else self._city()

        return Listing(
            source=self.name,
            url=href,
            listing_id=listing_id,
            title=title,
            price_eur=price,
            # Card price is labelled "incl. some utilities" -> treat as warm.
            price_type="warm",
            rooms=rooms,
            furnished=True,           # furnished / mid-term marketplace
            address_or_area=area,
            lat=None, lng=None,        # geocoded downstream from the address
            area_sqm=size,
            extra={"availability": availability, "price_label": price_label},
        )

    def scrape(self) -> list[Listing]:
        listings: list[Listing] = []
        seen_ids: set[str] = set()
        for page in range(1, max(1, self.source_cfg.max_pages) + 1):
            url = self._page_url(page)
            try:
                resp = self.get(url)
            except Exception as exc:
                logger.warning("[housinganywhere] page %d failed: %s", page, exc)
                break
            tree = HTMLParser(resp.text)
            cards = tree.css('[data-test-locator="ListingCard"]')
            if not cards:
                logger.info("[housinganywhere] no cards on page %d — stopping", page)
                break
            new_on_page = 0
            for card in cards:
                lg = self._card_to_listing(card)
                if lg and lg.listing_id not in seen_ids:
                    seen_ids.add(lg.listing_id)
                    listings.append(lg)
                    new_on_page += 1
            if new_on_page == 0:  # pagination exhausted / repeating
                break
        logger.info("[housinganywhere] collected %d raw listings", len(listings))
        return listings
