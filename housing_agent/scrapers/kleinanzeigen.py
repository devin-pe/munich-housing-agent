"""Kleinanzeigen (formerly eBay Kleinanzeigen) scraper.

WHY HTTP: search results are server-rendered `article.aditem` cards parsed with
selectolax. We use category c203 (Mietwohnungen = whole apartments, NOT the WG
shared-room category c199) with a price path segment.

FURNISHED: Kleinanzeigen has no first-class furnished-only filter, so we keep only
listings whose title/tags mention "möbliert" (furnished=True); others are marked
furnished=False and dropped by the central filter.

Prices are usually Kaltmiete (or "VB" = negotiable → no price). Cards have no
coordinates → geocoded downstream.

MODULARITY: locations use a numeric code (l6411 = München); new cities need an
entry in CITY_CODES.

FRAGILE BITS (update if the site changes):
    - CITY_CODES, category c203, URL/pagination path segments
    - selectors: article.aditem[data-adid], .aditem-main--middle--price-shipping--price
"""
from __future__ import annotations

import logging
import re

from selectolax.parser import HTMLParser, Node

from ..models import Listing
from ._util import parse_end_date, parse_price_eur, parse_rooms, parse_sqm
from .base import BaseScraper

logger = logging.getLogger("housing_agent")

BASE = "https://www.kleinanzeigen.de"
# city key -> (city slug, location code)
CITY_CODES = {"munich": ("muenchen", "l6411"), "münchen": ("muenchen", "l6411"),
              "muenchen": ("muenchen", "l6411")}
_FURNISHED_RE = re.compile(r"möbl|furnished", re.I)


class KleinanzeigenScraper(BaseScraper):
    name = "kleinanzeigen"

    def _city(self) -> tuple[str, str] | None:
        return CITY_CODES.get(self.config.search.city.strip().lower())

    def _page_url(self, slug: str, code: str, page: int) -> str:
        s = self.config.search
        price = f"preis:0:{int(s.max_price_eur)}"
        seite = "" if page <= 1 else f"seite:{page}/"
        return f"{BASE}/s-wohnung-mieten/{slug}/{seite}{price}/c203{code}"

    def _card_to_listing(self, card: Node) -> Listing | None:
        listing_id = card.attributes.get("data-adid")
        href = card.attributes.get("data-href", "")
        if not listing_id or not href:
            return None
        title_node = card.css_first("a.ellipsis")
        title = (title_node.text().strip() if title_node else "Kleinanzeigen listing")
        price_node = card.css_first(".aditem-main--middle--price-shipping--price")
        price = parse_price_eur(price_node.text()) if price_node else None
        text = re.sub(r"\s+", " ", (card.text() or "")).strip()
        area_node = card.css_first(".aditem-main--top--left")
        area = (area_node.text().strip() if area_node else self.config.search.city.title())

        furnished = bool(_FURNISHED_RE.search(title) or _FURNISHED_RE.search(text))

        return Listing(
            source=self.name,
            url=BASE + href if href.startswith("/") else href,
            listing_id=str(listing_id),
            title=title[:140],
            price_eur=price,
            price_type="kalt",
            rooms=parse_rooms(title) or parse_rooms(text),
            furnished=furnished if furnished else False,  # keep only explicit möbliert
            address_or_area=re.sub(r"\s+", " ", area),
            lat=None, lng=None,
            area_sqm=parse_sqm(text),
            available_until=parse_end_date(text),
        )

    def scrape(self) -> list[Listing]:
        city = self._city()
        if not city:
            logger.warning("[kleinanzeigen] no location code for %r — add it to CITY_CODES. Skipping.",
                           self.config.search.city)
            return []
        slug, code = city
        listings: list[Listing] = []
        seen: set[str] = set()
        for page in range(1, max(1, self.source_cfg.max_pages) + 1):
            try:
                resp = self.get(self._page_url(slug, code, page))
            except Exception as exc:
                logger.warning("[kleinanzeigen] page %d failed: %s", page, exc)
                break
            tree = HTMLParser(resp.text)
            cards = tree.css("article.aditem")
            if not cards:
                break
            new = 0
            for card in cards:
                lg = self._card_to_listing(card)
                if lg and lg.listing_id not in seen:
                    seen.add(lg.listing_id)
                    listings.append(lg)
                    new += 1
            if new == 0:
                break
        logger.info("[kleinanzeigen] collected %d raw listings", len(listings))
        return listings
