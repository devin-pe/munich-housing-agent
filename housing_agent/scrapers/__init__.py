"""Scraper registry.

Each scraper subclasses BaseScraper and registers under its config key. The
pipeline instantiates only the sources marked enabled in config.yaml.
"""
from __future__ import annotations

from .base import BaseScraper
from .housinganywhere import HousingAnywhereScraper
from .immowelt import ImmoweltScraper
from .kleinanzeigen import KleinanzeigenScraper
from .mrlodge import MrLodgeScraper
from .spacest import SpacestScraper
from .wggesucht import WgGesuchtScraper
from .wunderflats import WunderflatsScraper

# Map config `sources:` keys -> scraper classes. To add a site: subclass
# BaseScraper in this package, return normalized Listing objects, and register it
# here with the same key used under `sources:` in config.yaml.
SCRAPER_REGISTRY: dict[str, type[BaseScraper]] = {
    "wunderflats": WunderflatsScraper,
    "housinganywhere": HousingAnywhereScraper,
    "spacest": SpacestScraper,
    "wggesucht": WgGesuchtScraper,
    "kleinanzeigen": KleinanzeigenScraper,
    "mrlodge": MrLodgeScraper,
    "immowelt": ImmoweltScraper,   # bot-protected; disabled by default
}

__all__ = ["BaseScraper", "SCRAPER_REGISTRY"]
