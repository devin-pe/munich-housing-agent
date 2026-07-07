"""End-to-end orchestration: scrape → attribute-filter → commute-filter → dedup → email.

Resilience contract:
  * A scraper raising never aborts the run; its source is recorded and reported in
    the digest footer.
  * The digest is still sent when some sources failed (as long as email is
    configured), so you always get whatever we could gather.
  * Listings are marked "sent" in the dedup store ONLY after a successful send.
"""
from __future__ import annotations

import logging

from .commute import CommuteFilter
from .config import Config
from .emailer import Emailer
from .filters import apply_filters
from .geocode import Geocoder
from .models import Listing
from .scrapers import SCRAPER_REGISTRY
from .store import SeenStore

logger = logging.getLogger("housing_agent")


def run_scrapers(config: Config) -> tuple[list[Listing], list[str]]:
    all_listings: list[Listing] = []
    failed: list[str] = []
    for src in config.enabled_sources():
        scraper_cls = SCRAPER_REGISTRY.get(src.name)
        if scraper_cls is None:
            logger.warning("Source '%s' enabled but not implemented — skipping", src.name)
            failed.append(src.name)
            continue
        logger.info("Scraping %s (engine=%s)", src.name, src.engine)
        try:
            with scraper_cls(config, src) as scraper:
                all_listings.extend(scraper.scrape())
        except Exception as exc:
            logger.exception("Scraper '%s' failed: %s", src.name, exc)
            failed.append(src.name)
    return all_listings, failed


def run(config: Config, dry_run: bool = False) -> int:
    logger.info("=== Housing Agent run start (dry_run=%s) ===", dry_run)

    listings, failed_sources = run_scrapers(config)
    logger.info("Scraped %d raw listings; failed sources: %s",
                len(listings), failed_sources or "none")

    # 1. Attribute filter (price/rooms/furnished + warm normalization)
    listings, _ = apply_filters(listings, config)

    # 2. Commute filter (≤ max_commute_minutes; unknown kept & flagged)
    geocoder = Geocoder(config)
    commute = CommuteFilter(config, geocoder=geocoder)
    listings, _ = commute.annotate_and_filter(listings)
    geocoder.save()

    # 3. Dedup — only listings we've never sent
    store = SeenStore(config.runtime.data_dir)
    new_listings = store.filter_new(listings)

    # 4. Email
    if not new_listings and not config.email.send_when_empty:
        logger.info("No new listings and send_when_empty=false — not sending.")
        store.close()
        return 0

    if dry_run:
        _write_preview(config, new_listings, failed_sources)
        store.close()
        return 0

    emailer = Emailer(config)
    ok = emailer.send(new_listings, failed_sources)
    if ok:
        store.mark_sent(new_listings)   # record ONLY after a successful send
    store.close()
    return 0 if ok else 1


def _write_preview(config: Config, listings: list[Listing], failed_sources: list[str]) -> None:
    """Dry-run: render the digest to files instead of sending."""
    from pathlib import Path
    from .emailer import render_html, render_plaintext

    out_dir = Path(config.runtime.data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "digest_preview.html").write_text(render_html(listings, failed_sources), encoding="utf-8")
    (out_dir / "digest_preview.txt").write_text(render_plaintext(listings, failed_sources), encoding="utf-8")
    logger.info("[dry-run] Wrote %d listings to %s/digest_preview.{html,txt} (no email sent)",
                len(listings), out_dir)
