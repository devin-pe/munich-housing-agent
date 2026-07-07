"""Entry point:  python -m housing_agent [options]

Phase 2 status: `--scrape-only` is fully wired. The full pipeline (commute
filter, dedup, email) lands in Phases 3–6.
"""
from __future__ import annotations

import argparse
import json
import sys

# Ensure German umlauts / € print on Windows terminals (default cp1252 would crash).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from .config import load_config
from .logging_setup import setup_logging
from .pipeline import run as run_pipeline, run_scrapers


def _print_listings(listings) -> None:
    """Human-readable dump of normalized listings (Phase 2 verification)."""
    if not listings:
        print("\n(no listings returned)\n")
        return
    print(f"\n=== {len(listings)} normalized listings ===\n")
    for i, lg in enumerate(listings, 1):
        price = f"€{lg.price_eur:,.0f}" if lg.price_eur is not None else "n/a"
        rooms = lg.rooms if lg.rooms is not None else "?"
        coords = f"{lg.lat:.5f},{lg.lng:.5f}" if lg.lat and lg.lng else "no-coords"
        print(f"{i:>3}. [{lg.source}] {lg.title[:70]}")
        print(f"     {price} ({lg.price_type}) | {rooms} Zi | "
              f"{lg.area_sqm or '?'} m² | furnished={lg.furnished} | {coords}")
        print(f"     {lg.address_or_area}")
        print(f"     {lg.url}\n")


def _check_commute(config) -> int:
    """Run the configured transit provider against a few known Munich points so you
    can sanity-check travel times to the office anchor."""
    from .commute import CommuteFilter
    from .geocode import Geocoder

    filt = CommuteFilter(config, geocoder=Geocoder(config))
    if filt.anchor is None:
        print("No anchor coordinates resolved — set anchor_lat/lng in config.yaml.")
        return 1
    if not filt.provider.available:
        print("Google Maps API key missing — set GOOGLE_MAPS_API_KEY in .env "
              "(with the Routes API enabled).")
        return 1

    print(f"Provider : {filt.provider.name}")
    print(f"Office    : {config.search.anchor_address} {filt.anchor}")
    print(f"Arrive by : {filt.arrival.isoformat()} ({filt.arrival.strftime('%A')})")
    print(f"Limit     : {config.search.max_commute_minutes} min\n")

    # (name, lat, lng, rough expected minutes) — sanity references, not assertions.
    refs = [
        ("Marienplatz (central)",     48.1374, 11.5755, "~10-15"),
        ("Hauptbahnhof (central)",    48.1402, 11.5600, "~15-20"),
        ("Sendlinger Tor",            48.1339, 11.5674, "~15-20"),
        ("Garching Forschungszentrum",48.2650, 11.6710, "~30-40"),
        ("Bad Tölz (~50km south)",    47.7607, 11.5560, ">60"),
    ]
    for name, lat, lng, expect in refs:
        m = filt.provider.travel_minutes((lat, lng), filt.anchor, filt.arrival)
        got = f"{m} min" if m is not None else "unknown"
        print(f"  {name:30} -> {got:>10}   (expected {expect})")
    filt.cache.save()
    return 0


def _test_email(config, logger) -> int:
    """Send a sample digest so you can confirm your email transport works."""
    from .emailer import Emailer
    from .models import Listing

    sample = [
        Listing(source="wunderflats", url="https://wunderflats.com/en", listing_id="demo1",
                title="TEST — Bright möbliert 1-Zimmer near Universität", price_eur=1290,
                price_type="warm", rooms=1, furnished=True, address_or_area="Schwabing, München",
                lat=48.156, lng=11.58, area_sqm=32, warm_price_eur=1290, commute_minutes=12),
        Listing(source="wunderflats", url="https://wunderflats.com/en", listing_id="demo2",
                title="TEST — 2-Zimmer with balcony (Kalt+NK estimate)", price_eur=1150,
                price_type="kalt", rooms=2, furnished=True, address_or_area="Maxvorstadt, München",
                lat=48.15, lng=11.57, area_sqm=48, warm_price_eur=1400,
                price_is_estimated=True, commute_minutes=21),
    ]
    ok = Emailer(config).send(sample, failed_sources=[])
    if ok:
        print(f"Test email sent to {config.email.recipient} via {config.email.transport}.")
        return 0
    print("Test email FAILED — check .env credentials and config.yaml email.transport. "
          "See the log for details.")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="housing_agent", description=__doc__)
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--scrape-only", action="store_true",
                        help="Only run scrapers and print normalized results (no filter/email)")
    parser.add_argument("--json", action="store_true",
                        help="With --scrape-only, print listings as JSON")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run the full pipeline but do not send email (Phase 4+)")
    parser.add_argument("--check-commute", action="store_true",
                        help="Sanity-check the configured transit provider against known "
                             "Munich reference points and print door-to-door times")
    parser.add_argument("--test-email", action="store_true",
                        help="Send a small sample digest to the configured recipient "
                             "to verify email credentials")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    logger = setup_logging(config.runtime.log_level, config.runtime.data_dir)

    if args.check_commute:
        return _check_commute(config)

    if args.test_email:
        return _test_email(config, logger)

    if args.scrape_only:
        listings, failed = run_scrapers(config)
        if args.json:
            print(json.dumps([lg.to_dict() for lg in listings], ensure_ascii=False, indent=2))
        else:
            _print_listings(listings)
        if failed:
            logger.warning("Sources that failed: %s", ", ".join(failed))
        return 0

    # Full daily pipeline: scrape → filter → commute → dedup → email.
    return run_pipeline(config, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
