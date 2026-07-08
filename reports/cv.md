# Ranker cross-validation (stratified 5-fold)

n=106 (53 pos / 53 neg), features=['metro_walk_min', 'price_per_m2', 'price_eur', 'aesthetic_score', 'desc_pca_0', 'desc_pca_1']

| model | ROC-AUC (mean±std) | PR-AUC | accuracy | per-fold AUC |
|---|---|---|---|---|
| heuristic | 0.461 ± 0.128 | 0.553 ± 0.056 | 0.462 ± 0.055 | 0.39, 0.65, 0.40, 0.30, 0.57 |
| logreg | 0.647 ± 0.092 | 0.660 ± 0.072 | 0.594 ± 0.081 | 0.74, 0.56, 0.63, 0.76, 0.54 |
| catboost | 0.652 ± 0.108 | 0.687 ± 0.051 | 0.584 ± 0.121 | 0.74, 0.55, 0.63, 0.81, 0.53 |
Winner: **catboost** (AUC 0.652) vs heuristic 0.461.

## Interpretability
CatBoost feature importances:
  - price_eur: 39.8
  - aesthetic_score: 17.6
  - desc_pca_0: 17.5
  - desc_pca_1: 8.9
  - metro_walk_min: 8.2
  - price_per_m2: 7.9
