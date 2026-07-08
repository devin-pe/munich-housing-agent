"""Phase 1 — snapshot listing URLs into a local cache (manifest + images).

Reads data/positives.txt and data/negatives.txt, fetches each listing DETAIL page
over plain HTTP, extracts compact fields, downloads images, and writes
data/manifest.jsonl. After this runs, the dataset lives entirely in the cache —
later phases never touch the live URLs again.

Blocked/dead pages (kleinanzeigen, wg-gesucht, immowelt are DataDome-protected):
logged and skipped, UNLESS you drop a saved page at data/manual/<listing_id>.html,
which the parser will read instead of the live URL.

Usage:  python -m ranker.snapshot
"""
from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path

import httpx
from selectolax.parser import HTMLParser

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
IMG_DIR = DATA / "images"
MANUAL_DIR = DATA / "manual"
MANIFEST = DATA / "manifest.jsonl"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}
MAX_IMAGES = 12
BLOCK_MARKERS = ("ich bin kein roboter", "are you human", "captcha", "datadome",
                 "px-captcha", "access denied", "request unsuccessful")

# Mr. Lodge listing_id -> price EUR, built at startup from the daily agent's
# working search-card scraper (detail pages render rent via JS, so we can't parse it).
MRLODGE_PRICES: dict[str, float] = {}


def _build_mrlodge_prices() -> None:
    """Populate MRLODGE_PRICES by reusing the daily agent's Mr. Lodge scraper.
    Best-effort: covers currently-listed apartments; rented/removed ones stay None."""
    try:
        from housing_agent.config import load_config, SourceConfig
        from housing_agent.scrapers import MrLodgeScraper
        cfg = load_config()
        src = SourceConfig(name="mrlodge", enabled=True, engine="http", max_pages=15)
        with MrLodgeScraper(cfg, src) as s:
            for lg in s.scrape():
                if lg.price_eur:
                    MRLODGE_PRICES[f"mrlodge-{lg.listing_id}"] = lg.price_eur
        print(f"Mr. Lodge price map: {len(MRLODGE_PRICES)} listings priced from search cards\n")
    except Exception as exc:
        print(f"[warn] could not build Mr. Lodge price map: {exc}\n")


@dataclass
class Record:
    listing_id: str
    url: str
    label: int
    site: str
    fetch_status: str = "pending"        # ok | blocked | dead | manual | error
    local_image_paths: list[str] = field(default_factory=list)
    description: str = ""
    price_eur: float | None = None
    price_type: str = "unknown"          # warm | kalt | unknown
    size_m2: float | None = None
    rooms: float | None = None
    address: str = ""
    posted_date: str | None = None


# ── URL → (site, id) ─────────────────────────────────────────────────────────
def site_and_id(url: str) -> tuple[str, str]:
    u = url.strip()
    if "mrlodge.com" in u:
        m = re.search(r"-(\d+)(?:[/?#]|$)", u)
        return "mrlodge", f"mrlodge-{m.group(1) if m else _hash(u)}"
    if "wg-gesucht.de" in u:
        m = re.search(r"\.(\d+)\.html", u)
        return "wggesucht", f"wggesucht-{m.group(1) if m else _hash(u)}"
    if "immowelt.de" in u:
        m = re.search(r"/expose/([A-Za-z0-9]+)", u)
        return "immowelt", f"immowelt-{m.group(1) if m else _hash(u)}"
    if "wunderflats.com" in u:
        m = re.search(r"/([0-9a-f]{24})(?:[/?#]|$)", u)
        return "wunderflats", f"wunderflats-{m.group(1) if m else _hash(u)}"
    if "kleinanzeigen.de" in u:
        m = re.search(r"/(\d+)-\d+-\d+", u) or re.search(r"-(\d+)(?:[/?#]|$)", u)
        return "kleinanzeigen", f"kleinanzeigen-{m.group(1) if m else _hash(u)}"
    if "spacest.com" in u:
        m = re.search(r"/rent-listing/(\d+)", u)
        return "spacest", f"spacest-{m.group(1) if m else _hash(u)}"
    if "housinganywhere.com" in u:
        m = re.search(r"/room/(ut\d+)", u)
        return "housinganywhere", f"housinganywhere-{m.group(1) if m else _hash(u)}"
    return "other", f"other-{_hash(u)}"


def _hash(s: str) -> str:
    import hashlib
    return hashlib.sha1(s.encode()).hexdigest()[:12]


# ── fetch ────────────────────────────────────────────────────────────────────
def load_page(rec: Record, client: httpx.Client) -> tuple[str, str]:
    """Return (html, status). Prefers a manual saved page if present."""
    manual = MANUAL_DIR / f"{rec.listing_id}.html"
    if manual.exists():
        return manual.read_text(encoding="utf-8", errors="replace"), "manual"
    try:
        r = client.get(rec.url)
    except Exception as exc:
        print(f"  [error] {rec.listing_id}: {exc}")
        return "", "error"
    if r.status_code in (401, 403, 429):
        return "", "blocked"
    if r.status_code in (404, 410):
        return "", "dead"
    if r.status_code != 200:
        return "", "error"
    html = r.text
    low = html.lower()
    # Only treat as blocked if this is an actual challenge page. Real listing pages
    # are large and full of content; an incidental "captcha" script reference in a
    # 200 KB page is NOT a block (that false-positived all of WG-Gesucht before).
    m = re.search(r"<title[^>]*>(.*?)</title>", low, re.S)
    title = m.group(1) if m else ""
    challenge_titles = ("ich bin kein roboter", "just a moment", "attention required",
                        "are you human", "access denied", "pardon our interruption",
                        "bot verification")
    if any(c in title for c in challenge_titles):
        return "", "blocked"
    if len(html) < 15000 and any(mk in low for mk in BLOCK_MARKERS):
        return "", "blocked"
    return html, "ok"


# ── generic extraction (JSON-LD + OG + meta + text) ──────────────────────────
def _jsonld(tree: HTMLParser) -> list[dict]:
    out = []
    for n in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(n.text())
        except (json.JSONDecodeError, TypeError):
            continue
        out.extend(data if isinstance(data, list) else [data])
    return out


def _meta(tree: HTMLParser, *names: str) -> str:
    for name in names:
        for attr in ("property", "name", "itemprop"):
            n = tree.css_first(f'meta[{attr}="{name}"]')
            if n and n.attributes.get("content"):
                return n.attributes["content"].strip()
    return ""


def extract(rec: Record, html: str) -> None:
    tree = HTMLParser(html)
    ld = _jsonld(tree)
    text = re.sub(r"\s+", " ", tree.body.text() if tree.body else tree.text())

    # description
    rec.description = (_ld_field(ld, "description") or _meta(tree, "og:description", "description")
                       or "")[:4000]
    sd = _structured_site_data(rec.site, html)   # price/rooms/size from inline JSON
    low = text.lower()

    # price — Mr. Lodge renders rent via JS (detail HTML has no usable price, and
    # the generic parser grabs the object-number instead), so we look it up from the
    # search-card price map built at startup. Other sites: inline JSON / JSON-LD /
    # labelled text.
    if rec.site == "mrlodge":
        price = MRLODGE_PRICES.get(rec.listing_id)
    else:
        price = sd.get("price")
        if price is None:
            p = _ld_price(ld)
            price = p if (p and 150 <= p <= 20000) else _generic_price(text)
    rec.price_eur = price
    rec.price_type = ("warm" if ("warm" in low or "gesamtmiete" in low or "all-inclusive" in low)
                      else "kalt" if ("kalt" in low or "nettokalt" in low) else "unknown")

    # size (m²) and rooms — inline JSON first, else text regex, with sanity clamps.
    size = sd.get("size")
    if size is None:
        ms = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:m²|m2|qm)", text)
        size = _to_float(ms.group(1)) if ms else None
    rec.size_m2 = size if (size and 5 <= size <= 400) else None

    rooms = sd.get("rooms")
    if rooms is None:
        mr = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:zimmer|zi\b|rooms?|bedroom)", low)
        rooms = _to_float(mr.group(1)) if mr else None
    rec.rooms = rooms if (rooms and 0.5 <= rooms <= 12) else None
    # address
    rec.address = _ld_address(ld) or _meta(tree, "og:street-address") or ""
    # images (generic + site-specific)
    imgs = _ld_images(ld) + _og_images(tree)
    imgs += SITE_IMAGE_HOOKS.get(rec.site, lambda t, h: [])(tree, html)
    rec.description = rec.description or _meta(tree, "og:title")
    return _dedupe(imgs)


def extract_and_images(rec: Record, html: str) -> list[str]:
    imgs = extract(rec, html)
    return imgs or []


# ── site-specific image hooks ────────────────────────────────────────────────
def _mrlodge_images(tree: HTMLParser, html: str) -> list[str]:
    out = []
    for n in tree.css("[data-pictures]"):
        try:
            for pic in json.loads(n.attributes.get("data-pictures", "[]")):
                p = pic.get("large") or pic.get("xmedium") or pic.get("medium")
                if p:
                    out.append(p if p.startswith("http") else "https://www.mrlodge.com" + p)
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def _spacest_images(tree: HTMLParser, html: str) -> list[str]:
    node = tree.css_first('script#__NEXT_DATA__')
    if not node:
        return []
    try:
        j = json.loads(node.text())
    except json.JSONDecodeError:
        return []
    out = []
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("image", "url", "large", "medium") and isinstance(v, str) and v.startswith("http") and re.search(r"\.(jpg|jpeg|png|webp)", v, re.I):
                    out.append(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for e in o:
                walk(e)
    walk(j)
    return out


def _wunderflats_images(tree: HTMLParser, html: str) -> list[str]:
    return _dedupe(re.findall(r"https://listingimages\.wunderflats\.com/[A-Za-z0-9_-]+-(?:original|large)[^\"'\s]*", html))


def _dedupe_photos(urls: list[str], key) -> list[str]:
    """Keep one URL per unique photo (galleries repeat each photo at many sizes).
    Un-escapes HTML entities in URLs (&amp; -> &)."""
    seen, out = set(), []
    for u in urls:
        u = u.replace("&amp;", "&")
        k = key(u)
        if k not in seen:
            seen.add(k)
            out.append(u)
    return out


def _immowelt_images(tree: HTMLParser, html: str) -> list[str]:
    # Real photos are on mms.immowelt.de (the ?ci_seal token is needed to fetch).
    urls = re.findall(r"https://mms\.immowelt\.de/[^\s\"'<>]+?\.jpe?g[^\s\"'<>]*", html, re.I)
    return _dedupe_photos(urls, key=lambda u: u.split("?")[0])   # dedupe by photo UUID


def _wggesucht_images(tree: HTMLParser, html: str) -> list[str]:
    # img.wg-gesucht.de/media/up/.../<64-hex>_name.sized.jpg (variants share the hex).
    urls = re.findall(r"https://img\.wg-gesucht\.de/media/[^\s\"'<>]+?\.jpe?g", html, re.I)
    def photo_id(u):
        m = re.search(r"/([0-9a-f]{40,})", u)
        return m.group(1) if m else u
    # prefer the larger ".sized" variant when both exist
    urls.sort(key=lambda u: (".sized." not in u, u))
    return _dedupe_photos(urls, key=photo_id)


def _housinganywhere_images(tree: HTMLParser, html: str) -> list[str]:
    # housinganywhere.imgix.net/unit_type/<id>/<uuid>.jpg?<resize params>
    urls = re.findall(r"https://housinganywhere\.imgix\.net/[^\s\"'<>]+?\.jpe?g[^\s\"'<>]*", html, re.I)
    return _dedupe_photos(urls, key=lambda u: u.split("?")[0])   # dedupe by photo path


SITE_IMAGE_HOOKS = {
    "mrlodge": _mrlodge_images,
    "spacest": _spacest_images,
    "wunderflats": _wunderflats_images,
    "immowelt": _immowelt_images,
    "wggesucht": _wggesucht_images,
    "housinganywhere": _housinganywhere_images,
}


# ── JSON-LD helpers ──────────────────────────────────────────────────────────
def _ld_field(ld, key):
    for d in ld:
        v = d.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _ld_price(ld):
    for d in ld:
        offers = d.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        p = offers.get("price") if isinstance(offers, dict) else None
        if p is not None:
            return _to_float(str(p))
    return None


def _ld_address(ld):
    for d in ld:
        a = d.get("address")
        if isinstance(a, dict):
            parts = [a.get("streetAddress"), a.get("postalCode"), a.get("addressLocality")]
            s = ", ".join(str(p) for p in parts if p)   # postalCode is sometimes an int
            if s:
                return s
        elif isinstance(a, str) and a.strip():
            return a.strip()
    return ""


def _ld_images(ld):
    out = []
    for d in ld:
        im = d.get("image")
        if isinstance(im, str):
            out.append(im)
        elif isinstance(im, list):
            out.extend(x if isinstance(x, str) else x.get("url", "") for x in im)
        elif isinstance(im, dict):
            out.append(im.get("url", ""))
    return [x for x in out if x]


def _og_images(tree):
    out = []
    for attr in ("og:image", "og:image:secure_url", "twitter:image"):
        for n in tree.css(f'meta[property="{attr}"], meta[name="{attr}"]'):
            c = n.attributes.get("content")
            if c:
                out.append(c)
    return out


def _dedupe(seq):
    seen, out = set(), []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _parse_eur(s):
    """Locale-aware EUR amount. German uses '.' for thousands and ',' for decimals
    ('1.090' = 1090, '35,58' = 35.58); English '3,450' = 3450. Getting this wrong
    turned '35,58 €' (a €/m² figure) into 3558 and picked it as the rent."""
    s = re.sub(r"[^\d.,]", "", str(s))
    if not s:
        return None
    if "." in s and "," in s:                      # 1.234,56 -> 1234.56
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".") if re.search(r",\d{2}$", s) else s.replace(",", "")
    elif "." in s:                                  # decimal only if exactly 2 trailing digits
        if not (re.search(r"\.\d{2}$", s) and len(s.split(".")[0]) <= 2):
            s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


# Prefer warm/total rent labels, then any plausible "<n> €". Rents clamp to
# [150, 20000] to skip deposits-in-thousands, €/m² figures, IDs, postcodes.
_RENT_LABELS = ("gesamtmiete", "warmmiete", "warm", "all-inclusive", "bruttomiete",
                "kaltmiete", "nettokalt", "grundmiete", "miete", "per month",
                "/month", "/monat", "pro monat", "rent")


def _generic_price(text: str) -> float | None:
    low = text.lower()
    for label in _RENT_LABELS:
        for m in re.finditer(re.escape(label) + r"[^0-9]{0,25}([\d][\d.,]{1,8})\s*€", low):
            v = _parse_eur(m.group(1))
            if v and 150 <= v <= 20000:
                return v
    for m in re.finditer(r"([\d][\d.,]{1,8})\s*€", text):
        v = _parse_eur(m.group(1))
        if v and 150 <= v <= 20000:
            return v
    return None


def _structured_site_data(site: str, html: str) -> dict:
    """Price/rooms/size from a site's inline JSON, where the visible page renders
    them via JS (Wunderflats, HousingAnywhere, Spacest embed the numbers as JSON)."""
    d: dict = {}
    try:
        if site == "wunderflats":
            m = re.search(r'<script[^>]*application/json[^>]*>(.*?)</script>', html, re.S)
            if m:
                lst = (json.loads(m.group(1)).get("pageData") or {}).get("listing") or {}
                if isinstance(lst.get("price"), (int, float)):
                    d["price"] = round(lst["price"] / 100, 2)   # cents -> EUR
                if isinstance(lst.get("rooms"), (int, float)):
                    d["rooms"] = lst["rooms"]
                if isinstance(lst.get("area"), (int, float)):
                    d["size"] = lst["area"]
        elif site == "housinganywhere":
            m = re.search(r'"price"\s*:\s*(\d{4,7})\b', html)
            if m:
                d["price"] = round(int(m.group(1)) / 100, 2)    # cents -> EUR
        elif site == "spacest":
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
            if m:
                j = json.loads(m.group(1))
                def find(o):
                    if isinstance(o, dict):
                        for k in ("monthlyPrice", "price"):
                            v = o.get(k)
                            if isinstance(v, (int, float)) and 150 <= v <= 20000:
                                return float(v)
                        for v in o.values():
                            r = find(v)
                            if r:
                                return r
                    elif isinstance(o, list):
                        for e in o:
                            r = find(e)
                            if r:
                                return r
                    return None
                p = find(j.get("props") or j)
                if p:
                    d["price"] = p
    except (json.JSONDecodeError, TypeError, KeyError, ValueError):
        pass
    return d


def _to_float(s):
    s = re.sub(r"[^\d.,]", "", str(s))
    if not s:
        return None
    s = s.replace(".", "").replace(",", ".") if s.count(",") == 1 and s.rfind(",") > s.rfind(".") else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


# ── image download ───────────────────────────────────────────────────────────
def download_images(rec: Record, urls: list[str], client: httpx.Client) -> None:
    dest = IMG_DIR / rec.listing_id
    # Reuse already-downloaded images (fast re-runs; images don't change).
    if dest.exists():
        existing = sorted(str(p.relative_to(ROOT)) for p in dest.glob("*")
                          if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
        if existing:
            rec.local_image_paths = existing
            return
    dest.mkdir(parents=True, exist_ok=True)
    saved = []
    for i, u in enumerate(urls[:MAX_IMAGES]):
        try:
            r = client.get(u, timeout=20)
            if r.status_code == 200 and len(r.content) > 3000:
                ext = ".jpg"
                if ".png" in u.lower():
                    ext = ".png"
                elif ".webp" in u.lower():
                    ext = ".webp"
                p = dest / f"{i:02d}{ext}"
                p.write_bytes(r.content)
                saved.append(str(p.relative_to(ROOT)))
        except Exception:
            continue
    rec.local_image_paths = saved


# ── main ─────────────────────────────────────────────────────────────────────
def read_urls(path: Path, label: int) -> list[Record]:
    recs = []
    if not path.exists():
        return recs
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or not line.startswith("http"):
            continue
        site, lid = site_and_id(line)
        recs.append(Record(listing_id=lid, url=line, label=label, site=site))
    return recs


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    records = read_urls(DATA / "positives.txt", 1) + read_urls(DATA / "negatives.txt", 0)
    # de-dupe by listing_id (keep first / positive precedence via order)
    seen, uniq = set(), []
    for r in records:
        if r.listing_id not in seen:
            seen.add(r.listing_id)
            uniq.append(r)
    print(f"Loaded {len(uniq)} unique listings ({sum(r.label for r in uniq)} pos / "
          f"{sum(1 for r in uniq if r.label == 0)} neg)\n")

    _build_mrlodge_prices()   # id->price map for Mr. Lodge (JS-rendered detail pages)

    IMG_DIR.mkdir(parents=True, exist_ok=True)
    MANUAL_DIR.mkdir(parents=True, exist_ok=True)
    client = httpx.Client(headers=HEADERS, follow_redirects=True, timeout=30)

    with MANIFEST.open("w", encoding="utf-8") as fh:
        for i, rec in enumerate(uniq, 1):
            html, status = load_page(rec, client)
            rec.fetch_status = status
            if html:
                try:
                    imgs = extract_and_images(rec, html)
                    download_images(rec, imgs, client)
                    if status == "manual":
                        pass
                    elif not rec.local_image_paths and rec.price_eur is None:
                        rec.fetch_status = "ok"  # parsed but thin
                except Exception as exc:
                    print(f"  [parse-error] {rec.listing_id}: {exc}")
                    rec.fetch_status = "error"
            print(f"[{i:>3}/{len(uniq)}] {rec.fetch_status:>7} {rec.site:>14} "
                  f"imgs={len(rec.local_image_paths):>2} €{rec.price_eur or '-'} "
                  f"{rec.size_m2 or '-'}m² {rec.listing_id}")
            fh.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
            if status not in ("manual",):
                time.sleep(1.2)  # polite throttle for live fetches
    client.close()

    _attrition(uniq)
    return 0


def _attrition(recs: list[Record]) -> None:
    from collections import Counter
    print("\n===== ATTRITION REPORT =====")
    for label, name in ((1, "positives"), (0, "negatives")):
        sub = [r for r in recs if r.label == label]
        by_status = Counter(r.fetch_status for r in sub)
        usable = sum(1 for r in sub if r.fetch_status in ("ok", "manual") and (r.local_image_paths or r.price_eur))
        print(f"\n{name} (n={len(sub)}): usable={usable}")
        for st, c in by_status.most_common():
            print(f"    {st:>8}: {c}")
    print("\nPer-site status:")
    sites = sorted({r.site for r in recs})
    for site in sites:
        sub = [r for r in recs if r.site == site]
        ok = sum(1 for r in sub if r.fetch_status in ("ok", "manual"))
        print(f"    {site:>14}: {ok}/{len(sub)} fetched")
    print(f"\nManifest: {MANIFEST.relative_to(ROOT)}  |  images: {IMG_DIR.relative_to(ROOT)}/")
    print("Blocked/dead? Save the page as data/manual/<listing_id>.html and re-run.")


if __name__ == "__main__":
    raise SystemExit(main())
