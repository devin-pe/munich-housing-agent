# Ranker cross-validation (stratified 5-fold)

n=106 (53 pos / 53 neg), features=['metro_walk_min', 'walk_to_office_min', 'price_per_m2', 'price_eur', 'aesthetic_score', 'desc_pca_0', 'desc_pca_1']

| model | ROC-AUC (mean±std) | PR-AUC | accuracy | per-fold AUC |
|---|---|---|---|---|
| heuristic | 0.502 ± 0.122 | 0.558 ± 0.078 | 0.490 ± 0.070 | 0.44, 0.65, 0.48, 0.32, 0.63 |
| logreg | 0.625 ± 0.106 | 0.642 ± 0.085 | 0.584 ± 0.050 | 0.74, 0.55, 0.55, 0.77, 0.53 |
| catboost | 0.664 ± 0.094 | 0.731 ± 0.058 | 0.537 ± 0.091 | 0.80, 0.53, 0.64, 0.73, 0.63 |
Winner: **catboost** (AUC 0.664) vs heuristic 0.502.

## Interpretability
CatBoost feature importances:
  - price_eur: 38.1
  - aesthetic_score: 15.6
  - desc_pca_0: 13.7
  - desc_pca_1: 10.8
  - walk_to_office_min: 9.2
  - price_per_m2: 7.4
  - metro_walk_min: 5.3
