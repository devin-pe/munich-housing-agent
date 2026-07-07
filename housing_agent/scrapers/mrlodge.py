"""Mr. Lodge scraper — Munich furnished-apartment agency.

WHY HTTP: the search page (/rentals/<city>/apartments) is server-rendered. Each
listing is a `div.card` wrapping a link `/rent/<slug>-<id>`; price, rooms (German
Zimmer), size and district live in the card text. Mr. Lodge is a furnished-rental
agency, so listings are furnished and the monthly price is the all-inclusive
(warm) rent. Cards have no coordinates → geocoded downstream from the district.

FRAGILE BITS (update if the site changes):
    - SEARCH_URL path
    - card selector `div.card` + the `/rent/…-<id>` link
    - the text regexes for price (…€), rooms ("… room/Zimmer"), size ("… m²")
"""
from __future__ import annotations

import logging
import re

from selectolax.parser import HTMLParser, Node

from ..models import Listing
from ._util import parse_price_eur, parse_rooms, parse_sqm
from .base import BaseScraper

logger = logging.getLogger("housing_agent")

BASE = "https://www.mrlodge.com"
_ID_RE = re.compile(r"-(\d+)$")
_PRICE_NEAR_EUR = re.compile(r"([\d.,]+)\s*€")


class MrLodgeScraper(BaseScraper):
    name = "mrlodge"

    def _search_url(self, page: int) -> str:
        city = self.config.search.city.strip().lower()
        # Pagination via ?page= appears to be client-side (same HTML returned), so
        # we fetch the first page; boutique inventory is small anyway.
        return f"{BASE}/rentals/{city}/apartments"

    def _card_to_listing(self, card: Node) -> Listing | None:
        link = card.css_first('a[href^="/rent/"]')
        if not link:
            return None
        href = link.attributes.get("href", "")
        m = _ID_RE.search(href)
        if not m:
            return None
        listing_id = m.group(1)
        url = BASE + href

        text = re.sub(r"\s+", " ", (card.text() or "")).strip()
        rooms = parse_rooms(text)
        sqm = parse_sqm(text)
        price_m = _PRICE_NEAR_EUR.search(text)
        price = parse_price_eur(price_m.group(0)) if price_m else None

        # Title = text before the room/size figures; district = between size and price.
        title = re.split(r"\s*\d+(?:[.,]\d+)?\s*(?:room|zimmer)", text, maxsplit=1, flags=re.I)[0]
        title = title.replace("Video", "").strip() or "Mr. Lodge apartment"
        area_m = re.search(r"m²\s*(.+?)\s*[\d.,]+\s*€", text)
        area = area_m.group(1).strip() if area_m else self.config.search.city.title()

        return Listing(
            source=self.name,
            url=url,
            listing_id=listing_id,
            title=title[:140],
            price_eur=price,
            price_type="warm",     # furnished agency: all-inclusive monthly rent
            rooms=rooms,           # German Zimmer already
            furnished=True,
            address_or_area=area,
            lat=None, lng=None,    # geocoded downstream from the district
            area_sqm=sqm,
        )

    def scrape(self) -> list[Listing]:
        listings: list[Listing] = []
        seen: set[str] = set()
        try:
            resp = self.get(self._search_url(1))
        except Exception as exc:
            logger.warning("[mrlodge] fetch failed: %s", exc)
            return []
        tree = HTMLParser(resp.text)
        for card in tree.css("div.card"):
            lg = self._card_to_listing(card)
            if lg and lg.listing_id not in seen:
                seen.add(lg.listing_id)
                listings.append(lg)
        logger.info("[mrlodge] collected %d raw listings", len(listings))
        return listings
