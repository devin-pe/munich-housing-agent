"""Load and validate configuration from config.yaml + .env.

Secrets come from environment (.env); everything else from config.yaml.
Access is via typed dataclasses so the rest of the code never touches raw dicts.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass
class SearchConfig:
    anchor_address: str
    anchor_lat: float | None
    anchor_lng: float | None
    city: str            # e.g. "munich", "berlin" — drives the search URLs
    country: str         # e.g. "Germany" — used by sources that need it (HousingAnywhere)
    max_commute_minutes: int
    arrival_time_local: str
    max_price_eur: float
    min_rooms: int
    max_rooms: int
    furnished_required: bool
    nebenkosten_estimate_eur: float
    move_in_date: str | None
    earliest_move_in: str | None
    min_lease_months: int | None
    min_available_until: str | None   # drop listings whose lease ends before this (ISO date)


@dataclass
class SourceConfig:
    name: str
    enabled: bool
    engine: str          # "http" (all current sources use plain HTTP)
    max_pages: int


@dataclass
class CommuteConfig:
    region: str          # country bias for Google (e.g. "de")
    cache_ttl_days: int


@dataclass
class EmailConfig:
    transport: str       # resend | smtp
    recipient: str
    sender_name: str
    sender_address: str
    subject_prefix: str
    send_when_empty: bool


@dataclass
class DigestConfig:
    mode: str            # by_site | ranked
    rank_walk_weight: float   # ranked mode: 0=pure model P(good), 1=pure office-proximity


@dataclass
class RuntimeConfig:
    request_delay_seconds: float
    request_timeout_seconds: float
    data_dir: str
    log_level: str


@dataclass
class Secrets:
    """Values sourced from the environment (.env). Missing ones are empty strings."""
    google_maps_api_key: str = ""
    resend_api_key: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""


@dataclass
class Config:
    search: SearchConfig
    sources: dict[str, SourceConfig]
    commute: CommuteConfig
    email: EmailConfig
    runtime: RuntimeConfig
    digest: DigestConfig
    secrets: Secrets = field(default_factory=Secrets)

    def enabled_sources(self) -> list[SourceConfig]:
        return [s for s in self.sources.values() if s.enabled]


def _load_secrets() -> Secrets:
    return Secrets(
        google_maps_api_key=os.getenv("GOOGLE_MAPS_API_KEY", ""),
        resend_api_key=os.getenv("RESEND_API_KEY", ""),
        smtp_host=os.getenv("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=int(os.getenv("SMTP_PORT", "587") or "587"),
        smtp_username=os.getenv("SMTP_USERNAME", ""),
        smtp_password=os.getenv("SMTP_PASSWORD", ""),
    )


def load_config(path: str | Path = "config.yaml") -> Config:
    """Read config.yaml and .env, returning a validated Config."""
    load_dotenv()  # populate os.environ from .env if present

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path.resolve()}. "
            "Copy the provided config.yaml into the project root."
        )
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    s = raw.get("search", {})
    search = SearchConfig(
        anchor_address=s["anchor_address"],
        anchor_lat=s.get("anchor_lat"),
        anchor_lng=s.get("anchor_lng"),
        city=s.get("city", "munich"),
        country=str(s.get("country", "Germany")),
        max_commute_minutes=int(s.get("max_commute_minutes", 40)),
        arrival_time_local=str(s.get("arrival_time_local", "09:00")),
        max_price_eur=float(s.get("max_price_eur", 1500)),
        min_rooms=int(s.get("min_rooms", 1)),
        max_rooms=int(s.get("max_rooms", 2)),
        furnished_required=bool(s.get("furnished_required", True)),
        nebenkosten_estimate_eur=float(s.get("nebenkosten_estimate_eur", 250)),
        move_in_date=(str(s["move_in_date"]) if s.get("move_in_date") else None),
        earliest_move_in=(str(s["earliest_move_in"]) if s.get("earliest_move_in") else None),
        min_lease_months=(int(s["min_lease_months"]) if s.get("min_lease_months") else None),
        min_available_until=(str(s["min_available_until"]) if s.get("min_available_until") else None),
    )

    sources: dict[str, SourceConfig] = {}
    for name, cfg in (raw.get("sources") or {}).items():
        sources[name] = SourceConfig(
            name=name,
            enabled=bool(cfg.get("enabled", False)),
            engine=str(cfg.get("engine", "http")),
            max_pages=int(cfg.get("max_pages", 3)),
        )

    c = raw.get("commute", {})
    commute = CommuteConfig(
        region=str(c.get("region", "de")),
        cache_ttl_days=int(c.get("cache_ttl_days", 30)),
    )

    e = raw.get("email", {})
    email = EmailConfig(
        transport=str(e.get("transport", "smtp")),
        recipient=e["recipient"],
        sender_name=str(e.get("sender_name", "Housing Agent")),
        sender_address=str(e.get("sender_address", "onboarding@resend.dev")),
        subject_prefix=str(e.get("subject_prefix", "Housing")),
        send_when_empty=bool(e.get("send_when_empty", False)),
    )


    r = raw.get("runtime", {})
    runtime = RuntimeConfig(
        request_delay_seconds=float(r.get("request_delay_seconds", 2.5)),
        request_timeout_seconds=float(r.get("request_timeout_seconds", 30)),
        data_dir=str(r.get("data_dir", "data")),
        log_level=str(r.get("log_level", "INFO")),
    )

    d = raw.get("digest", {})
    digest = DigestConfig(mode=str(d.get("mode", "by_site")),
                          rank_walk_weight=float(d.get("rank_walk_weight", 0.4)))

    return Config(
        search=search,
        sources=sources,
        commute=commute,
        email=email,
        runtime=runtime,
        digest=digest,
        secrets=_load_secrets(),
    )
