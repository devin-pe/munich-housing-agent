"""Shared parsing helpers for scrapers (prices, rooms, sizes).

German vs English number formats differ across sites, so these are deliberately
tolerant. Prices are whole euros in practice, so we strip all separators; rooms
can be fractional (e.g. "2.5 Zimmer") so we preserve the decimal.
"""
from __future__ import annotations

import re

_PRICE_RE = re.compile(r"(\d[\d.,]*)")
_ROOMS_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:zimmer|zi\b|rooms?|room)", re.I)
_SQM_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:m²|m2|qm|sqm|m\b)", re.I)


def parse_price_eur(text: str | None) -> float | None:
    """Parse a monthly price from text like '1.450 €', '3,450 €/Month', '1180 €'.
    Returns None for 'VB'/'auf Anfrage'/no number. Prices are treated as whole
    euros, so thousand separators (both '.' and ',') are removed."""
    if not text:
        return None
    m = _PRICE_RE.search(text.replace("\xa0", " "))
    if not m:
        return None
    digits = re.sub(r"[.,]", "", m.group(1))
    return float(digits) if digits.isdigit() else None


def parse_rooms(text: str | None) -> float | None:
    """Parse a (possibly fractional) room count from '2,5 Zimmer', '1 room', etc."""
    if not text:
        return None
    m = _ROOMS_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def parse_sqm(text: str | None) -> float | None:
    """Parse a living-space value in m² from '35 m²', '105 m2', '76 qm'."""
    if not text:
        return None
    m = _SQM_RE.search(text.replace("\xa0", " "))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None
