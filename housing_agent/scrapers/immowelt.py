"""Immowelt scraper — DISABLED by default.

⚠️ Immowelt runs DataDome. A plain HTTP request sometimes succeeds (HTTP 200,
server-rendered cards) but often returns a 403/CAPTCHA challenge, especially from
datacenter IPs (e.g. CI / cloud runners). So it's disabled in config.yaml and,
when enabled, fails gracefully (logs, returns []) if challenged — never breaking
the run. (Immonet is the same AVIV platform and is hard-blocked, so we don't ship
an Immonet scraper.)

Prices are Kaltmiete. Furnished isn't a card field, so we keep only listings whose
title mentions "möbliert" (others are marked furnished=False and dropped).
Cards have no coordinates → geocoded downstream.

FRAGILE BITS: URL params (ami/ama rooms, pma price), the data-testid selectors.
"""
from __future__ import annotations

import logging
import re

from selectolax.parser import HTMLParser, Node

from ..models import Listing
from ._util import parse_price_eur, parse_rooms, parse_sqm
from .base import BaseScraper

logger = logging.getLogger("housing_agent")

BASE = "https://www.immowelt.de"
_FURNISHED_RE = re.compile(r"möbl|furnished", re.I)
_EXPOSE_RE = re.compile(r"/expose/([A-Za-z0-9]+)")


class ImmoweltScraper(BaseScraper):
    name = "immowelt"

    def _page_url(self, page: int) -> str:
        s = self.config.search
        city = s.city.strip().lower()
        url = (f"{BASE}/suche/{city}/wohnungen/mieten"
               f"?ami={s.min_rooms}&ama={s.max_rooms}&pma={int(s.max_price_eur)}")
        return url if page <= 1 else f"{url}&sp={page}"

    @staticmethod
    def _blocked(resp_text: str) -> bool:
        low = resp_text.lower()
        return "captcha-delivery" in low or "datadome" in low or "ich bin kein roboter" in low

    def _card_to_listing(self, card: Node) -> Listing | None:
        link = card.css_first('a[data-testid="card-mfe-covering-link-testid"][href]')
        if not link:
            link = card.css_first('a[href*="/expose/"]')
        if not link:
            return None
        href = link.attributes.get("href", "")
        url = href if href.startswith("http") else BASE + href
        m = _EXPOSE_RE.search(href)
        listing_id = m.group(1) if m else url

        def loc(name: str) -> str:
            n = card.css_first(f'[data-testid="{name}"]')
            return n.text().strip() if n else ""

        keyfacts = loc("cardmfe-keyfacts-testid")
        title = loc("cardmfe-description-box-address") or "Immowelt apartment"
        text = re.sub(r"\s+", " ", (card.text() or "")).strip()
        furnished = bool(_FURNISHED_RE.search(text))

        return Listing(
            source=self.name,
            url=url,
            listing_id=str(listing_id),
            title=title[:140],
            price_eur=parse_price_eur(loc("cardmfe-price-testid")),
            price_type="kalt",
            rooms=parse_rooms(keyfacts),
            furnished=furnished if furnished else False,
            address_or_area=loc("cardmfe-description-box-address") or self.config.search.city.title(),
            lat=None, lng=None,
            area_sqm=parse_sqm(keyfacts),
        )

    def scrape(self) -> list[Listing]:
        listings: list[Listing] = []
        seen: set[str] = set()
        for page in range(1, max(1, self.source_cfg.max_pages) + 1):
            try:
                resp = self.get(self._page_url(page))
            except Exception as exc:
                logger.warning("[immowelt] page %d failed (likely bot-blocked): %s", page, exc)
                break
            if self._blocked(resp.text):
                logger.warning("[immowelt] DataDome challenge on page %d — skipping source.", page)
                break
            tree = HTMLParser(resp.text)
            cards = tree.css('[data-testid^="classified-card-"]')
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
        logger.info("[immowelt] collected %d raw listings", len(listings))
        return listings
