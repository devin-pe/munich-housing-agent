"""WG-Gesucht scraper.

WHY HTTP: the search results are server-rendered cards we parse with selectolax.
We use the `1-zimmer-wohnungen` category (self-contained 1-room apartments — NOT
the `wg-zimmer` shared-room category), with the furnished (fur=1) and max-rent
(rMax) filters applied in the URL.

Prices are Kaltmiete (cold rent); the central filter estimates Warmmiete by adding
the configured Nebenkosten and flags it. Cards have no coordinates → geocoded
downstream from the district/street.

MODULARITY: WG-Gesucht identifies cities by a numeric city_id AND a name slug, so
new cities need an entry in CITY_IDS below.

FRAGILE BITS (update if the site changes):
    - CITY_IDS, the URL template, and the page segment (4th dotted number, 0-based)
    - card selector `.wgg_card.offer_list_item` + `a.detailansicht`
"""
from __future__ import annotations

import logging
import re

from selectolax.parser import HTMLParser, Node

from ..models import Listing
from ._util import parse_price_eur, parse_rooms, parse_sqm
from .base import BaseScraper

logger = logging.getLogger("housing_agent")

BASE = "https://www.wg-gesucht.de"
# city key -> (name slug used in the URL, numeric city_id)
CITY_IDS = {"munich": ("Muenchen", 90), "münchen": ("Muenchen", 90), "muenchen": ("Muenchen", 90)}


class WgGesuchtScraper(BaseScraper):
    name = "wggesucht"

    def _city(self) -> tuple[str, int] | None:
        return CITY_IDS.get(self.config.search.city.strip().lower())

    def _page_url(self, name: str, city_id: int, page: int) -> str:
        # Path numbers are category.cityId.rentType.page.filter (page is 0-based).
        s = self.config.search
        return (f"{BASE}/1-zimmer-wohnungen-in-{name}.{city_id}.1.1.{page}.html"
                f"?offer_filter=1&city_id={city_id}&rMax={int(s.max_price_eur)}"
                f"&fur=1&categories[0]=1")

    def _card_to_listing(self, card: Node) -> Listing | None:
        listing_id = card.attributes.get("data-id") or card.attributes.get("data-ad_id")
        link = card.css_first("a.detailansicht[href]")
        if not link:
            return None
        href = link.attributes.get("href", "")
        url = href if href.startswith("http") else BASE + "/" + href.lstrip("/")
        if not listing_id:
            m = re.search(r"\.(\d+)\.html", href)
            listing_id = m.group(1) if m else None
        if not listing_id:
            return None

        title = (link.attributes.get("title") or link.text() or "").replace(
            "Anzeige ansehen:", "").strip() or "WG-Gesucht apartment"
        text = re.sub(r"\s+", " ", (card.text() or "")).strip()

        # Price: anchor to the € sign — parsing the whole card text would grab the
        # "1" from "1-Zimmer-Wohnung" instead of the rent.
        price_m = re.search(r"([\d.,]+)\s*€", text)
        price = parse_price_eur(price_m.group(0)) if price_m else None

        # Area: the detail URL encodes city+district, e.g.
        # /1-zimmer-wohnungen-in-Muenchen-Maxvorstadt.1234.html -> "Muenchen, Maxvorstadt".
        area = self.config.search.city.title()
        dm = re.search(r"-in-(.+?)\.\d+\.html", href)
        if dm:
            area = dm.group(1).replace("-", ", ")

        return Listing(
            source=self.name,
            url=url,
            listing_id=str(listing_id),
            title=title[:140],
            price_eur=price,
            price_type="kalt",     # WG-Gesucht "Miete" is cold rent
            rooms=parse_rooms(title) or 1,   # 1-zimmer category
            furnished=True,        # fur=1 filter
            address_or_area=area,
            lat=None, lng=None,    # geocoded downstream
            area_sqm=parse_sqm(text),
        )

    def scrape(self) -> list[Listing]:
        city = self._city()
        if not city:
            logger.warning("[wggesucht] no city_id for %r — add it to CITY_IDS. Skipping.",
                           self.config.search.city)
            return []
        name, city_id = city
        listings: list[Listing] = []
        seen: set[str] = set()
        for page in range(max(1, self.source_cfg.max_pages)):
            try:
                resp = self.get(self._page_url(name, city_id, page))
            except Exception as exc:
                logger.warning("[wggesucht] page %d failed: %s", page, exc)
                break
            tree = HTMLParser(resp.text)
            cards = tree.css(".wgg_card.offer_list_item")
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
        logger.info("[wggesucht] collected %d raw listings", len(listings))
        return listings
