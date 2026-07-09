"""Phase 5 — score_listing(): the daily agent's ranking hook.

Loads models/scorer.joblib (winner model + fitted feature pipeline) and turns a
listing record into P(good). Computes each of the 5 features from whatever the
record provides and median-imputes the rest (imputation medians are saved in the
artifact). Heavy encoders (CLIP/MiniLM) are imported lazily and only when the
record actually carries images / description — so scoring a plain daily Listing
(price + address, no photos) stays light and needs no torch at call time.

    from ranker.score import score_listing
    p = score_listing(listing_record)   # 0..1, higher = better

`listing_record` may be a dict or any object with attributes. Recognized fields:
    warm_price_eur / price_eur, area_sqm / size_m2, lat, lng,
    address_or_area / address, description, image_paths (local), image_urls.
"""
from __future__ import annotations

import functools
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCORER = ROOT / "models" / "scorer.joblib"


class ScorerUnavailable(RuntimeError):
    """Raised when the model artifact is missing or unreadable."""


def _get(rec, *names, default=None):
    for n in names:
        if isinstance(rec, dict):
            if rec.get(n) is not None:
                return rec[n]
        elif getattr(rec, n, None) is not None:
            return getattr(rec, n)
    return default


@functools.lru_cache(maxsize=1)
def _load_artifact():
    if not SCORER.exists():
        raise ScorerUnavailable(f"{SCORER} not found — run `python -m ranker.train` first.")
    import joblib
    return joblib.load(SCORER)


_METRO_CACHE = None


def _latlng(rec):
    """Listing coordinates: from the record if present, else geocode its address."""
    lat, lng = _get(rec, "lat"), _get(rec, "lng")
    if lat is not None and lng is not None:
        return (lat, lng)
    addr = _get(rec, "address_or_area", "address")
    if not addr:
        return None
    import sys
    sys.path.insert(0, str(ROOT))
    from housing_agent.config import load_config
    from housing_agent.geocode import Geocoder
    return Geocoder(load_config()).geocode(addr)


def _metro(rec, latlng):
    global _METRO_CACHE
    from .features import metro_walk_min, METRO_CACHE, _load_json
    if _METRO_CACHE is None:                     # load persistent cache once and reuse
        _METRO_CACHE = _load_json(METRO_CACHE)
    return metro_walk_min(latlng, _METRO_CACHE) if latlng else None


def _walk_to_office(latlng):
    import sys
    sys.path.insert(0, str(ROOT))
    from housing_agent.config import load_config
    from .features import walk_to_office_min
    cfg = load_config()
    return walk_to_office_min(latlng, (cfg.search.anchor_lat, cfg.search.anchor_lng))


def _aesthetic(rec):
    paths = _get(rec, "image_paths", default=[])
    urls = _get(rec, "image_urls", default=[])
    if not paths and not urls:
        return None
    from .features import Encoders, aesthetic_score
    import numpy as np
    from PIL import Image
    import io, httpx, torch
    model, preprocess = Encoders.clip()
    embs = []
    for p in paths:
        try:
            img = Image.open(ROOT / p if not str(p).startswith("/") else p).convert("RGB")
        except Exception:
            continue
        embs.append(_encode(model, preprocess, img, torch))
    for u in urls[:12]:
        try:
            r = httpx.get(u, timeout=20)
            if r.status_code == 200:
                embs.append(_encode(model, preprocess, Image.open(io.BytesIO(r.content)).convert("RGB"), torch))
        except Exception:
            continue
    if not embs:
        return None
    return aesthetic_score(np.mean(embs, axis=0))


def _encode(model, preprocess, img, torch):
    with torch.no_grad():
        e = model.encode_image(preprocess(img).unsqueeze(0))
        e = e / e.norm(dim=-1, keepdim=True)
    return e.squeeze(0).cpu().numpy()


def _desc_pca(rec, art):
    text = _get(rec, "description")
    if not text:
        return None, None
    from .features import Encoders
    v = Encoders.text().encode(text, normalize_embeddings=True)
    xy = art["pca_desc"].transform([v])[0]
    return float(xy[0]), float(xy[1])


def score_listing(rec) -> float:
    """Return P(good) in [0, 1]; higher = better. Raises ScorerUnavailable if the
    model artifact is missing (caller should fall back to unranked)."""
    art = _load_artifact()
    medians = art["medians"]

    price = _get(rec, "warm_price_eur", "price_eur")
    size = _get(rec, "area_sqm", "size_m2")
    ppm = round(price / size, 2) if (price and size) else None

    latlng = _safe(_latlng, rec)                 # resolve coordinates once
    feats = {
        "metro_walk_min": _safe(lambda r: _metro(r, latlng), rec),
        "walk_to_office_min": _safe(lambda r: _walk_to_office(latlng), rec),
        "price_per_m2": ppm,
        "price_eur": price,
        "aesthetic_score": _safe(_aesthetic, rec),
    }
    d0, d1 = _safe(lambda r: _desc_pca(r, art), rec, default=(None, None))
    feats["desc_pca_0"], feats["desc_pca_1"] = d0, d1

    x = [feats[c] if feats[c] is not None else medians[c] for c in art["feature_cols"]]
    return float(art["model"].predict_proba([x])[:, 1][0])


def _safe(fn, rec, default=None):
    try:
        return fn(rec)
    except Exception:
        return default


def scorer_available() -> bool:
    try:
        _load_artifact()
        return True
    except Exception:
        return False
