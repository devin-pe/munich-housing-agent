"""Phase 2 — build the compact feature matrix from the snapshot cache.

Five features (one per type), chosen to stay well-regularized at n≈107:
    metro_walk_min   location  — walking minutes to nearest U-/S-Bahn/tram stop
    price_per_m2     price     — warm rent / m²
    aesthetic_score  images    — LAION aesthetic head on mean-pooled CLIP ViT-B/32
    desc_pca_0/1     text      — PCA-2 of all-MiniLM-L6-v2 description embedding

Frozen encoders, inference-only, CPU. All embeddings are cached by listing_id so
re-runs are cheap. Missing values are median-imputed (medians persisted for
inference); no rows are dropped. Outputs data/features.csv and persists the fitted
pipeline (PCA + medians + metadata) to models/feature_pipeline.joblib.

Usage:  python -m ranker.features
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MANIFEST = DATA / "manifest.jsonl"
CACHE = DATA / "cache"
CLIP_CACHE = CACHE / "clip"
TEXT_CACHE = CACHE / "text"
METRO_CACHE = CACHE / "metro.json"
MODELS = ROOT / "models"
AESTHETIC_WEIGHTS = MODELS / "aesthetic_vit_b_32.pth"
AESTHETIC_URL = "https://github.com/LAION-AI/aesthetic-predictor/raw/main/sa_0_4_vit_b_32_linear.pth"
FEATURES_CSV = DATA / "features.csv"
PIPELINE = MODELS / "feature_pipeline.joblib"

WALK_M_PER_MIN = 80.0        # ≈4.8 km/h
METRO_SEARCH_RADIUS_M = 1500
FEATURE_COLS = ["metro_walk_min", "walk_to_office_min", "price_per_m2", "price_eur",
                "aesthetic_score", "desc_pca_0", "desc_pca_1"]


# ── lazy encoders (loaded once) ──────────────────────────────────────────────
class Encoders:
    _clip = _clip_pre = _text = _aes = None

    @classmethod
    def clip(cls):
        if cls._clip is None:
            import open_clip, torch
            # Use the -quickgelu variant: OpenAI CLIP was trained with QuickGELU, and
            # the LAION aesthetic head expects those embeddings. Plain "ViT-B-32" +
            # openai loads with GELU (a mismatch that degrades the embeddings).
            model, _, preprocess = open_clip.create_model_and_transforms(
                "ViT-B-32-quickgelu", pretrained="openai")
            model.eval()
            cls._clip, cls._clip_pre = model, preprocess
            cls._torch = torch
        return cls._clip, cls._clip_pre

    @classmethod
    def aesthetic_head(cls):
        """LAION linear aesthetic predictor for CLIP ViT-B/32 (512 -> 1)."""
        if cls._aes is None:
            import torch
            import httpx
            if not AESTHETIC_WEIGHTS.exists():
                MODELS.mkdir(parents=True, exist_ok=True)
                r = httpx.get(AESTHETIC_URL, timeout=60, follow_redirects=True)
                r.raise_for_status()
                AESTHETIC_WEIGHTS.write_bytes(r.content)
            head = torch.nn.Linear(512, 1)
            state = torch.load(AESTHETIC_WEIGHTS, map_location="cpu", weights_only=True)
            head.load_state_dict(state)
            head.eval()
            cls._aes = head
        return cls._aes

    @classmethod
    def text(cls):
        if cls._text is None:
            from sentence_transformers import SentenceTransformer
            cls._text = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2", device="cpu")
        return cls._text


# ── image / aesthetic ────────────────────────────────────────────────────────
def clip_embedding(listing_id: str, image_paths: list[str]):
    """Mean-pooled, L2-normalized CLIP ViT-B/32 embedding across a listing's photos."""
    cache = CLIP_CACHE / f"{listing_id}.npy"
    if cache.exists():
        return np.load(cache)
    from PIL import Image
    import torch
    model, preprocess = Encoders.clip()
    embs = []
    for rel in image_paths:
        p = ROOT / rel
        try:
            img = Image.open(p).convert("RGB")
        except Exception:
            continue
        with torch.no_grad():
            t = preprocess(img).unsqueeze(0)
            e = model.encode_image(t)
            e = e / e.norm(dim=-1, keepdim=True)
        embs.append(e.squeeze(0).cpu().numpy())
    if not embs:
        return None
    v = np.mean(embs, axis=0).astype(np.float32)
    CLIP_CACHE.mkdir(parents=True, exist_ok=True)
    np.save(cache, v)
    return v


def aesthetic_score(clip_vec) -> float | None:
    """Combined aesthetic over ALL of a listing's photos.

    `clip_vec` is the mean of the per-image L2-normalized CLIP embeddings. The LAION
    head is linear, so head(mean_i e_i) == mean_i head(e_i) — i.e. the average of
    each photo's aesthetic score. We deliberately do NOT re-normalize the pooled
    vector (that would break the per-image averaging and bias toward listings with
    many similar photos)."""
    if clip_vec is None:
        return None
    import torch
    head = Encoders.aesthetic_head()
    with torch.no_grad():
        x = torch.from_numpy(np.asarray(clip_vec, dtype=np.float32)).unsqueeze(0)
        return float(head(x).item())


# ── description embedding ────────────────────────────────────────────────────
def text_embedding(listing_id: str, text: str):
    cache = TEXT_CACHE / f"{listing_id}.npy"
    if cache.exists():
        return np.load(cache)
    model = Encoders.text()
    v = model.encode(text or "", normalize_embeddings=True).astype(np.float32)
    TEXT_CACHE.mkdir(parents=True, exist_ok=True)
    np.save(cache, v)
    return v


# ── location: metro walking minutes (OSM Overpass, cached) ───────────────────
def _load_json(p: Path) -> dict:
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _haversine_m(a, b) -> float:
    R = 6371000.0
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def walk_to_office_min(latlng, anchor) -> float | None:
    """Straight-line walking minutes from the listing to the office anchor — a
    proximity-to-work signal distinct from walk-to-nearest-transit-stop."""
    if not latlng or not anchor or anchor[0] is None:
        return None
    return round(_haversine_m(latlng, anchor) / WALK_M_PER_MIN, 1)


def metro_walk_min(latlng, cache: dict) -> float | None:
    if not latlng:
        return None
    key = f"{latlng[0]:.4f},{latlng[1]:.4f}"
    if key in cache:
        return cache[key]
    import httpx
    la, lo = latlng
    q = (f"[out:json][timeout:25];("
         f"node[railway=station](around:{METRO_SEARCH_RADIUS_M},{la},{lo});"
         f"node[railway=halt](around:{METRO_SEARCH_RADIUS_M},{la},{lo});"
         f"node[railway=tram_stop](around:{METRO_SEARCH_RADIUS_M},{la},{lo});"
         f"node[station=subway](around:{METRO_SEARCH_RADIUS_M},{la},{lo});"
         f"node[public_transport=station](around:{METRO_SEARCH_RADIUS_M},{la},{lo});"
         f");out;")
    # Overpass 406s clients without a real User-Agent; also try a mirror on failure.
    headers = {"User-Agent": "housing-agent/0.1 (personal use; metro-distance feature)"}
    val = None
    for endpoint in ("https://overpass-api.de/api/interpreter",
                     "https://overpass.kumi.systems/api/interpreter",
                     "https://maps.mail.ru/osm/tools/overpass/api/interpreter"):
        try:
            r = httpx.post(endpoint, data={"data": q}, headers=headers, timeout=40)
            r.raise_for_status()
            els = r.json().get("elements", [])
            dists = [_haversine_m(latlng, (e["lat"], e["lon"])) for e in els if "lat" in e]
            val = round(min(dists) / WALK_M_PER_MIN, 1) if dists else None
            break
        except Exception as exc:
            print(f"  [overpass] {key} via {endpoint.split('/')[2]}: {exc}")
            continue
    if val is not None:
        cache[key] = val
        METRO_CACHE.write_text(json.dumps(cache), encoding="utf-8")
    return val


# ── main ─────────────────────────────────────────────────────────────────────
def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    import pandas as pd
    from sklearn.decomposition import PCA
    import joblib
    sys.path.insert(0, str(ROOT))
    from housing_agent.config import load_config
    from housing_agent.geocode import Geocoder

    rows = [json.loads(l) for l in MANIFEST.read_text(encoding="utf-8").splitlines() if l.strip()]
    rows = [r for r in rows if r["fetch_status"] in ("ok", "manual")]
    print(f"Building features for {len(rows)} listings...")

    cfg = load_config()
    geocoder = Geocoder(cfg)
    anchor = (cfg.search.anchor_lat, cfg.search.anchor_lng)
    metro_cache = _load_json(METRO_CACHE)

    recs, text_vecs = [], []
    for i, r in enumerate(rows, 1):
        lid = r["listing_id"]
        # price per m²
        price, size = r.get("price_eur"), r.get("size_m2")
        ppm = round(price / size, 2) if (price and size) else None
        # aesthetic
        aes = aesthetic_score(clip_embedding(lid, r.get("local_image_paths") or []))
        # description embedding (cached; PCA fitted after the loop)
        tv = text_embedding(lid, r.get("description") or "")
        text_vecs.append(tv)
        # location
        latlng = geocoder.geocode(r.get("address") or "") if r.get("address") else None
        metro = metro_walk_min(latlng, metro_cache)
        walk = walk_to_office_min(latlng, anchor)
        recs.append({"listing_id": lid, "label": r["label"], "site": r["site"],
                     "metro_walk_min": metro, "walk_to_office_min": walk,
                     "price_per_m2": ppm, "price_eur": price, "aesthetic_score": aes})
        print(f"[{i:>3}/{len(rows)}] {r['site']:>14} metro={metro} ppm={ppm} aes={round(aes,2) if aes else None}")
    geocoder.save()

    # PCA-2 of description embeddings
    X_text = np.vstack(text_vecs)
    pca = PCA(n_components=2, random_state=0).fit(X_text)
    desc_pca = pca.transform(X_text)
    for k, rec in enumerate(recs):
        rec["desc_pca_0"] = float(desc_pca[k, 0])
        rec["desc_pca_1"] = float(desc_pca[k, 1])

    df = pd.DataFrame(recs)
    # median-impute missing features (persist medians for inference)
    medians = {}
    for col in FEATURE_COLS:
        med = float(df[col].median())
        if not np.isfinite(med):        # entire column missing -> neutral 0, don't emit NaN
            print(f"  [warn] {col} is entirely missing; defaulting to 0.0")
            med = 0.0
        medians[col] = med
        n_missing = int(df[col].isna().sum())
        if n_missing:
            print(f"  imputing {n_missing} missing {col} -> median {round(med,2)}")
        df[col] = df[col].fillna(med)

    df.to_csv(FEATURES_CSV, index=False)
    MODELS.mkdir(parents=True, exist_ok=True)
    joblib.dump({"pca_desc": pca, "medians": medians, "feature_cols": FEATURE_COLS,
                 "clip_model": "ViT-B-32-quickgelu/openai", "text_model": "all-MiniLM-L6-v2"}, PIPELINE)

    print(f"\nWrote {FEATURES_CSV.relative_to(ROOT)} ({len(df)} rows) and "
          f"{PIPELINE.relative_to(ROOT)}")
    print("\nfeature summary (mean by label):")
    print(df.groupby("label")[FEATURE_COLS].mean().round(2).to_string())
    print(f"\nPCA-2 explained variance: {pca.explained_variance_ratio_.round(3).tolist()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
