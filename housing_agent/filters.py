"""Central, source-agnostic filtering + German-rent normalization.

Handles the German rental nuances explicitly:
  * Warmmiete vs Kaltmiete: we filter on the *warm* price (rent incl. utilities).
    If a listing only advertises Kaltmiete, we estimate warm = kalt + Nebenkosten
    (configurable) and flag the listing as an estimate.
  * Zimmer counting: "1-Zimmer" = studio, "2-Zimmer" = 1BR+living. A 1-bedroom
    target is therefore 1–2 Zimmer (min_rooms/max_rooms in config).
  * möbliert (furnished): required when furnished_required is set.
"""
from __future__ import annotations

import logging
import re

from .config import Config
from .models import Listing

logger = logging.getLogger("housing_agent")

# Text that indicates a student-only listing even when a structured flag is unset.
# Kept reasonably tight so "perfect for students and professionals" is NOT matched,
# while catching exclusive phrasing, enrollment requirements, and student dorms.
_STUDENT_ONLY_RE = re.compile(
    r"only\s+(?:for|available\s+for)\s+students"
    r"|exclusiv\w*\s+for\s+students"        # "exclusively for students"
    r"|students?\s+only"
    r"|only\s+students"
    r"|nur\s+f(?:ü|ue)r\s+studenten"
    r"|studenten\s+only"
    r"|(?:certificate|proof)\s+of\s+enroll?ment"   # student enrollment requirement
    r"|student(?:en)?wohnheim"              # student dormitory (inherently student-only)
    r"|student\s+(?:residence|hall|dorm|housing\s+only)",
    re.I,
)


def is_student_only_text(*texts: str | None) -> bool:
    """True if any of the given text fragments signals a student-only listing.
    Shared so scrapers with richer text (e.g. Spacest descriptions) can check too."""
    return any(_STUDENT_ONLY_RE.search(t) for t in texts if t)


# Text indicating a women/Frauen-only listing. Tight enough not to match neutral
# mentions ("close to Frauenkirche", "welcoming to women and men").
_WOMEN_ONLY_RE = re.compile(
    r"wom[ae]n\s+only"
    r"|only\s+(?:for\s+)?wom[ae]n"
    r"|for\s+wom[ae]n\s+only"
    r"|females?\s+only"
    r"|nur\s+(?:an\s+|f(?:ü|ue)r\s+)?frauen"    # "nur an/für Frauen", "nur Frauen"
    r"|frauen\s+only"
    r"|nur\s+weiblich",
    re.I,
)


def is_women_only_text(*texts: str | None) -> bool:
    """True if any text fragment signals a women/Frauen-only listing."""
    return any(_WOMEN_ONLY_RE.search(t) for t in texts if t)


# Blunt TITLE rules (per user request): drop if the title contains "student" in any
# form, or "frauen" — except when "frauen" is part of a Munich place name
# (Frauenkirche/Frauenplatz/Frauenstraße/…), which would be a false positive.
_STUDENT_TITLE_RE = re.compile(r"student", re.I)
_WOMEN_TITLE_RE = re.compile(
    r"\bwomen\b|\bfemale\b|weiblich"
    r"|frauen(?!kirche|platz|stra|str\.|tor|hof|chiemsee|insel|dorf|berg|feld)",
    re.I,
)


def _is_student_only(listing: Listing) -> bool:
    if listing.extra.get("only_students"):
        return True
    if _STUDENT_TITLE_RE.search(listing.title or ""):   # blunt: any "student*" in title
        return True
    # Careful phrasing check on richer text (e.g. Spacest descriptions), so a casual
    # "great for students" in a description doesn't over-trigger.
    return is_student_only_text(listing.extra.get("price_label"),
                                listing.extra.get("student_text"))


def _is_women_only(listing: Listing) -> bool:
    if _WOMEN_TITLE_RE.search(listing.title or ""):
        return True
    return is_women_only_text(listing.extra.get("price_label"),
                              listing.extra.get("student_text"))


# Parking/garage listings (not apartments). Drop when the title is about a
# garage/parking space AND does not mention a dwelling — so "Wohnung mit Garage"
# (an apartment that merely has a garage) is kept.
_PARKING_RE = re.compile(
    r"garage|stellplatz|tiefgarage|parkplatz|carport|duplexparker|parking", re.I)
_DWELLING_RE = re.compile(
    r"wohnung|apartment|appartement|zimmer|studio|\bflat\b|maisonette|penthouse|loft|"
    r"\bwg\b|\bhaus\b|\bhouse\b|home|residence", re.I)


def _is_garage(listing: Listing) -> bool:
    title = listing.title or ""
    return bool(_PARKING_RE.search(title)) and not _DWELLING_RE.search(title)


def compute_warm_price(listing: Listing, nebenkosten_estimate: float) -> None:
    """Populate listing.warm_price_eur and listing.price_is_estimated in-place."""
    if listing.price_eur is None:
        listing.warm_price_eur = None
        return
    if listing.price_type == "warm":
        listing.warm_price_eur = listing.price_eur
        listing.price_is_estimated = False
    elif listing.price_type == "kalt":
        listing.warm_price_eur = listing.price_eur + nebenkosten_estimate
        listing.price_is_estimated = True
    else:  # unknown — treat the advertised figure as warm but flag it
        listing.warm_price_eur = listing.price_eur
        listing.price_is_estimated = True


def apply_filters(listings: list[Listing], config: Config) -> tuple[list[Listing], dict]:
    """Keep only listings that satisfy price, rooms, and furnished criteria.

    Commute filtering is applied separately (commute.py). Returns (kept, stats).
    """
    s = config.search
    stats = {
        "input": len(listings),
        "dropped_price": 0,
        "dropped_rooms": 0,
        "dropped_furnished": 0,
        "dropped_no_price": 0,
        "dropped_student_only": 0,
        "dropped_women_only": 0,
        "dropped_garage": 0,
        "dropped_end_date": 0,
        "kept": 0,
    }
    kept: list[Listing] = []

    for lg in listings:
        compute_warm_price(lg, s.nebenkosten_estimate_eur)

        # Student-only listings are irrelevant for a working professional
        # (checks both the structured flag and student-only phrasing in the title).
        if _is_student_only(lg):
            stats["dropped_student_only"] += 1
            continue

        # Women/Frauen-only listings.
        if _is_women_only(lg):
            stats["dropped_women_only"] += 1
            continue

        # Garage / parking-space listings (not apartments).
        if _is_garage(lg):
            stats["dropped_garage"] += 1
            continue

        # Exclude fixed-term listings that end before the required date (short
        # sublets). Only applies when the source exposed an end date; unknown =
        # treated as available indefinitely and kept.
        if s.min_available_until and lg.available_until and lg.available_until < s.min_available_until:
            stats["dropped_end_date"] += 1
            continue

        if lg.warm_price_eur is None:
            stats["dropped_no_price"] += 1
            continue
        if lg.warm_price_eur > s.max_price_eur:
            stats["dropped_price"] += 1
            continue

        # Rooms: keep if unknown (don't hide a listing for missing metadata), else
        # enforce the 1–2 Zimmer window.
        if lg.rooms is not None and not (s.min_rooms <= lg.rooms <= s.max_rooms):
            stats["dropped_rooms"] += 1
            continue

        if s.furnished_required and lg.furnished is False:
            stats["dropped_furnished"] += 1
            continue

        kept.append(lg)

    stats["kept"] = len(kept)
    logger.info("Attribute filter: %s", stats)
    return kept, stats
