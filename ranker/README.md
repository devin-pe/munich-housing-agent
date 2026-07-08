# Listing Ranker (offline, CPU)

Trains a small binary classifier — good (1) / bad (0) — from two lists of listing
URLs, and exposes `score_listing()` so the daily agent can optionally rank a
digest by predicted fit. Pretrained encoders are used **inference-only** (no
fine-tuning); everything runs on CPU.

## The 5-feature vector (one per type)

Kept deliberately tiny for ~107 rows (58→53 usable positives) to avoid overfitting:

| type | feature |
|---|---|
| location | `metro_walk_min` — walk minutes to nearest U-/S-Bahn/tram (OSM Overpass) |
| price | `price_per_m2` — warm rent ÷ m² |
| images | `aesthetic_score` — LAION aesthetic head on mean-pooled CLIP ViT-B/32 |
| text | `desc_pca_0`, `desc_pca_1` — PCA-2 of all-MiniLM-L6-v2 description embedding |

## Install

```bash
python -m pip install -r ranker/requirements.txt   # torch(CPU)+CLIP+MiniLM+sklearn+catboost
```
Needs `GOOGLE_MAPS_API_KEY` in `.env` (Geocoding API) for the location feature.

## Run the pipeline

```bash
python -m ranker.snapshot    # 1. cache detail pages + images -> data/manifest.jsonl (+ attrition report)
python -m ranker.features    # 2. build features -> data/features.csv (+ models/feature_pipeline.joblib)
python -m ranker.train       # 3+4. heuristic + logreg + CatBoost, 5-fold CV -> models/scorer.joblib, reports/cv.md
```

- **Snapshot** reads `data/positives.txt` / `data/negatives.txt`. Blocked/dead
  pages are logged; drop a saved page at `data/manual/<listing_id>.html` to include
  one manually, then re-run. Everything is cached — later phases never hit the live
  URLs. (Mr. Lodge renders rent via JS, so its price is pulled from the daily
  agent's search-card scraper.)
- **Features** caches CLIP/MiniLM embeddings by `listing_id`; re-runs are cheap.
  Missing values are median-imputed (medians persisted for inference).
- **Train** reports ROC-AUC as **mean ± std across 5 folds** (the spread is real at
  this n and is shown), plus PR-AUC and accuracy, and prints coefficients /
  importances. The winner must be compared against the heuristic baseline.

> **Honest note:** at n≈107 with 5 coarse features the signal is weak — CV AUC ≈
> 0.55 and the learned models do not clearly beat the hand-weighted heuristic (see
> `reports/cv.md`). `aesthetic_score` and `metro_walk_min` carry most of it. Ranked
> mode is therefore a mild re-ordering, not a strong classifier. More labelled data
> and/or richer per-listing features would be needed to do better.

## How the daily agent uses it

Set `digest.mode` in the repo-root `config.yaml`:

- **`by_site`** (default) — listings grouped by source, unranked. The scorer is
  **not** loaded, so the daily agent runs with **none** of the ML deps installed.
- **`ranked`** — all surviving listings (after the hard filters: commute ≤ 40 min,
  warm ≤ €1500, furnished, 1–2 Zimmer) are pooled, scored with `score_listing()`,
  and sorted best-first with the score shown. If `models/scorer.joblib` is missing
  or fails to load, it logs a warning and **falls back to `by_site`**.

```python
from ranker.score import score_listing   # P(good), 0..1
```

Note: a live daily `Listing` has price + address (so `price_per_m2` and
`metro_walk_min` are computed) but no photos/description, so `aesthetic_score` and
the description features are median-imputed at scoring time. For CI ranked runs,
commit `models/scorer.joblib` + `models/feature_pipeline.joblib` and install
`ranker/requirements.txt` in the workflow.
