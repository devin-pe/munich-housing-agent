"""Phase 3+4 — train the ranker and report honest cross-validated metrics.

Models (CPU, small data):
  * Heuristic baseline (hand-weighted) — the bar a learned model must beat.
  * Logistic regression (L2, class_weight=balanced, standardized) — primary.
  * CatBoost (shallow, few iters, strong L2) — secondary; compared, not assumed.

Evaluation: stratified 5-fold CV. ROC-AUC as mean ± std across folds (the spread is
real at n≈107 and is shown, not hidden), plus PR-AUC and accuracy. Prints logreg
coefficients / CatBoost importances. Saves the winner + feature pipeline to
models/scorer.joblib and writes reports/cv.md.

Usage:  python -m ranker.train
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import joblib
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import StratifiedKFold
from sklearn.metrics import roc_auc_score, average_precision_score, accuracy_score

ROOT = Path(__file__).resolve().parent.parent
FEATURES_CSV = ROOT / "data" / "features.csv"
PIPELINE = ROOT / "models" / "feature_pipeline.joblib"
SCORER = ROOT / "models" / "scorer.joblib"
REPORT = ROOT / "reports" / "cv.md"
FEATURE_COLS = ["metro_walk_min", "walk_to_office_min", "price_per_m2", "price_eur",
                "aesthetic_score", "desc_pca_0", "desc_pca_1"]
# Heuristic direction: good = nicer photos, cheaper, closer to metro & to the office.
HEURISTIC_SIGNS = {"aesthetic_score": +1, "price_per_m2": -1, "price_eur": -1,
                   "metro_walk_min": -1, "walk_to_office_min": -1,
                   "desc_pca_0": 0, "desc_pca_1": 0}
SEED = 0


def cv_metrics(make_model, X, y, fit_predict=None):
    """Stratified 5-fold CV -> dict of per-fold + mean±std for AUC/PR-AUC/acc."""
    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=SEED)
    aucs, praucs, accs = [], [], []
    for tr, te in skf.split(X, y):
        proba = (fit_predict or _default_fit_predict)(make_model, X, y, tr, te)
        aucs.append(roc_auc_score(y[te], proba))
        praucs.append(average_precision_score(y[te], proba))
        accs.append(accuracy_score(y[te], (proba >= 0.5).astype(int)))
    return {"auc": aucs, "prauc": praucs, "acc": accs}


def _default_fit_predict(make_model, X, y, tr, te):
    m = make_model()
    m.fit(X[tr], y[tr])
    return m.predict_proba(X[te])[:, 1]


def _heuristic_fit_predict(_make, X, y, tr, te, cols=None):
    """No training: z-score by TRAIN stats, then a signed weighted sum on TEST."""
    mu, sd = X[tr].mean(axis=0), X[tr].std(axis=0) + 1e-9
    signs = np.array([HEURISTIC_SIGNS[c] for c in cols])
    score = ((X[te] - mu) / sd) @ signs
    return 1 / (1 + np.exp(-score))     # squash to [0,1] for AUC/threshold


def fmt(vals):
    return f"{np.mean(vals):.3f} ± {np.std(vals):.3f}"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    df = pd.read_csv(FEATURES_CSV)
    X = df[FEATURE_COLS].to_numpy(dtype=float)
    y = df["label"].to_numpy(dtype=int)
    print(f"Training on {len(df)} rows ({y.sum()} pos / {(y == 0).sum()} neg), "
          f"{len(FEATURE_COLS)} features\n")

    results = {}

    # 1. Heuristic baseline
    results["heuristic"] = cv_metrics(
        None, X, y, fit_predict=lambda m, X, y, tr, te: _heuristic_fit_predict(m, X, y, tr, te, FEATURE_COLS))

    # 2. Logistic regression (primary)
    def make_logreg():
        # L2 is the default penalty; not passing it avoids a sklearn 1.9 deprecation.
        return Pipeline([("scale", StandardScaler()),
                         ("lr", LogisticRegression(C=1.0, class_weight="balanced",
                                                   max_iter=1000))])
    results["logreg"] = cv_metrics(make_logreg, X, y)

    # 3. CatBoost (secondary, optional)
    have_catboost = False
    try:
        from catboost import CatBoostClassifier
        have_catboost = True
        def make_catboost():
            return CatBoostClassifier(depth=3, iterations=120, learning_rate=0.05,
                                      l2_leaf_reg=8, auto_class_weights="Balanced",
                                      random_seed=SEED, verbose=False)
        results["catboost"] = cv_metrics(make_catboost, X, y)
    except ImportError:
        print("[note] catboost not installed — skipping secondary model.\n")

    # ── report ───────────────────────────────────────────────────────────────
    lines = ["# Ranker cross-validation (stratified 5-fold)\n",
             f"n={len(df)} ({int(y.sum())} pos / {int((y==0).sum())} neg), "
             f"features={FEATURE_COLS}\n",
             "| model | ROC-AUC (mean±std) | PR-AUC | accuracy | per-fold AUC |",
             "|---|---|---|---|---|"]
    for name, r in results.items():
        perfold = ", ".join(f"{a:.2f}" for a in r["auc"])
        lines.append(f"| {name} | {fmt(r['auc'])} | {fmt(r['prauc'])} | {fmt(r['acc'])} | {perfold} |")
    report = "\n".join(lines)
    print(report)

    # pick winner by mean CV AUC among learned models; must beat heuristic
    learned = {k: v for k, v in results.items() if k != "heuristic"}
    winner_name = max(learned, key=lambda k: np.mean(learned[k]["auc"]))
    heur_auc = np.mean(results["heuristic"]["auc"])
    win_auc = np.mean(learned[winner_name]["auc"])
    verdict = (f"\nWinner: **{winner_name}** (AUC {win_auc:.3f}) vs heuristic {heur_auc:.3f}."
               + ("" if win_auc >= heur_auc else
                  "  ⚠️ Learned model does NOT beat the heuristic — features are weak at this n."))
    print(verdict)

    # ── interpretability + fit winner on all data ─────────────────────────────
    interp = ["\n## Interpretability"]
    if winner_name == "logreg":
        final = make_logreg().fit(X, y)
        coefs = final.named_steps["lr"].coef_[0]
        interp.append("Logistic-regression coefficients (standardized features):")
        for c, w in sorted(zip(FEATURE_COLS, coefs), key=lambda t: -abs(t[1])):
            interp.append(f"  - {c}: {w:+.3f}")
    else:
        final = make_catboost().fit(X, y)
        imps = final.get_feature_importance()
        interp.append("CatBoost feature importances:")
        for c, w in sorted(zip(FEATURE_COLS, imps), key=lambda t: -t[1]):
            interp.append(f"  - {c}: {w:.1f}")
    print("\n".join(interp))

    # ── persist winner + feature pipeline ──────────────────────────────────────
    fp = joblib.load(PIPELINE)
    joblib.dump({"model": final, "model_name": winner_name,
                 "pca_desc": fp["pca_desc"], "medians": fp["medians"],
                 "feature_cols": FEATURE_COLS,
                 "clip_model": fp["clip_model"], "text_model": fp["text_model"]}, SCORER)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(report + verdict + "\n" + "\n".join(interp) + "\n", encoding="utf-8")
    print(f"\nSaved winner to {SCORER.relative_to(ROOT)} and report to {REPORT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
