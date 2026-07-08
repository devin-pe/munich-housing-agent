"""Offline listing feature-extraction + ranking pipeline for the Munich housing agent.

Pipeline: snapshot.py -> features.py -> train.py -> score.py (score_listing).
All heavy ML deps live here so the daily agent's `by_site` digest mode runs without
them. See ranker/README.md.
"""
