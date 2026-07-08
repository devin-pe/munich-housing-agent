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

    # Defensive within-run de-duplication by dedup_key, so a source emitting the
    # same listing twice can never produce duplicate entries in the digest.
    unique: dict[str, Listing] = {}
    for lg in listings:
        unique.setdefault(lg.dedup_key(), lg)
    if len(unique) != len(listings):
        logger.info("Collapsed %d duplicate listing(s) within this run", len(listings) - len(unique))
    listings = list(unique.values())

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

    # 3b. Optional ranking (digest.mode: ranked). Falls back to by_site if the
    # offline scorer isn't available — and by_site never imports the ML pipeline.
    mode = config.digest.mode
    if mode == "ranked" and new_listings:
        mode = _rank_listings(new_listings)

    # 4. Email
    if not new_listings and not config.email.send_when_empty:
        logger.info("No new listings and send_when_empty=false — not sending.")
        store.close()
        return 0

    if dry_run:
        _write_preview(config, new_listings, failed_sources, mode)
        store.close()
        return 0

    emailer = Emailer(config)
    ok = emailer.send(new_listings, failed_sources, mode=mode)
    if ok:
        store.mark_sent(new_listings)   # record ONLY after a successful send
    store.close()
    return 0 if ok else 1


def _rank_listings(listings: list[Listing]) -> str:
    """Score each listing with the offline ranker and sort best-first (in place).
    Returns the effective digest mode: 'ranked' on success, else 'by_site'.

    Each survivor is enriched from its detail page (description + photos) so the
    ranker scores on real image/text features rather than median-imputing them.
    """
    try:
        from ranker.score import score_listing, scorer_available
        from ranker.enrich import enrich_listing
    except Exception as exc:
        logger.warning("ranked mode: ranker not importable (%s) — using by_site", exc)
        return "by_site"
    if not scorer_available():
        logger.warning("ranked mode: models/scorer.joblib missing — using by_site "
                       "(run `python -m ranker.train`)")
        return "by_site"

    scored = enriched = 0
    for lg in listings:
        rec = {"warm_price_eur": lg.warm_price_eur, "price_eur": lg.price_eur,
               "area_sqm": lg.area_sqm, "lat": lg.lat, "lng": lg.lng,
               "address_or_area": lg.address_or_area}
        try:                                    # detail-page description + images
            extra = enrich_listing(lg.url, lg.source)
            if extra.get("image_urls") or extra.get("description"):
                enriched += 1
            rec.update(extra)
        except Exception as exc:
            logger.debug("enrich failed for %s (%s)", lg.dedup_key(), exc)
        try:
            lg.extra["score"] = round(float(score_listing(rec)), 4)
            scored += 1
        except Exception as exc:
            logger.warning("scoring failed for %s (%s)", lg.dedup_key(), exc)
            lg.extra["score"] = 0.0
    listings.sort(key=lambda l: l.extra.get("score", 0.0), reverse=True)
    logger.info("Ranked %d/%d listings by P(good) (%d enriched from detail pages)",
                scored, len(listings), enriched)
    return "ranked"


def _write_preview(config: Config, listings: list[Listing], failed_sources: list[str],
                   mode: str = "by_site") -> None:
    """Dry-run: render the digest to files instead of sending."""
    from pathlib import Path
    from .emailer import render_html, render_plaintext

    out_dir = Path(config.runtime.data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "digest_preview.html").write_text(render_html(listings, failed_sources, mode), encoding="utf-8")
    (out_dir / "digest_preview.txt").write_text(render_plaintext(listings, failed_sources, mode), encoding="utf-8")
    logger.info("[dry-run] Wrote %d listings to %s/digest_preview.{html,txt} (no email sent)",
                len(listings), out_dir)
