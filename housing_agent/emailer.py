"""Compose and send the daily digest (HTML + plaintext), grouped by source.

Default transport is Resend; SMTP (Gmail app password) is a drop-in alternative
selected by config.yaml `email.transport`.
"""
from __future__ import annotations

import html
import logging
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx

from .config import Config
from .models import Listing

logger = logging.getLogger("housing_agent")

# Display names per source key. Unknown keys fall back to the key itself, so new
# scrapers work without touching this — add an entry only for nicer casing.
SOURCE_LABELS = {
    "wunderflats": "Wunderflats",
    "housinganywhere": "HousingAnywhere",
    "spacest": "Spacest",
    "wggesucht": "WG-Gesucht",
    "kleinanzeigen": "Kleinanzeigen",
    "mrlodge": "Mr. Lodge",
    "immowelt": "Immowelt",
}


# ─────────────────────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────────────────────
def _fmt_price(lg: Listing) -> str:
    if lg.warm_price_eur is None:
        return "price n/a"
    tag = " (est.)" if lg.price_is_estimated else ""
    return f"€{lg.warm_price_eur:,.0f}/mo warm{tag}"


def _fmt_commute(lg: Listing) -> str:
    if lg.commute_minutes is None:
        return "commute: unknown"
    return f"{lg.commute_minutes} min to office"


def _group_by_source(listings: list[Listing]) -> dict[str, list[Listing]]:
    grouped: dict[str, list[Listing]] = {}
    for lg in listings:
        grouped.setdefault(lg.source, []).append(lg)
    for items in grouped.values():
        # cheapest-commute first, then price
        items.sort(key=lambda x: (x.commute_minutes if x.commute_minutes is not None else 999,
                                   x.warm_price_eur or 1e9))
    return grouped


def _commute_known(listings: list[Listing]) -> bool:
    return any(lg.commute_minutes is not None for lg in listings)


def _score(lg: Listing):
    return lg.extra.get("score") if isinstance(lg.extra, dict) else None


def render_plaintext(listings: list[Listing], failed_sources: list[str], mode: str = "by_site") -> str:
    scope = "within commute range" if _commute_known(listings) else "matching your criteria"
    lines = [f"Munich furnished rentals — {date.today().isoformat()}",
             f"{len(listings)} new listing(s) {scope}.", ""]

    if mode == "ranked":
        lines.append("Ranked best-first by predicted fit (P good).\n")
        for i, lg in enumerate(listings, 1):   # already sorted by the pipeline
            sc = _score(lg)
            rooms = f"{lg.rooms:g} Zi" if lg.rooms is not None else "? Zi"
            area = f"{lg.area_sqm:g} m²" if lg.area_sqm else ""
            tag = f"[{sc:.0%}] " if isinstance(sc, (int, float)) else ""
            lines.append(f"{i}. {tag}{lg.title}")
            lines.append(f"    {_fmt_price(lg)} | {rooms} {area} | "
                         f"{SOURCE_LABELS.get(lg.source, lg.source)} | {_fmt_commute(lg)}")
            lines.append(f"    {lg.address_or_area}")
            lines.append(f"    {lg.url}")
        if failed_sources:
            lines.append("\nSources that failed today (skipped): " + ", ".join(failed_sources))
        return "\n".join(lines)
    for source, items in _group_by_source(listings).items():
        lines.append(f"== {SOURCE_LABELS.get(source, source)} ({len(items)}) ==")
        for lg in items:
            rooms = f"{lg.rooms:g} Zi" if lg.rooms is not None else "? Zi"
            area = f"{lg.area_sqm:g} m²" if lg.area_sqm else ""
            lines.append(f"- {lg.title}")
            lines.append(f"    {_fmt_price(lg)} | {rooms} {area} | "
                         f"{'möbliert' if lg.furnished else 'furnished?'} | {_fmt_commute(lg)}")
            lines.append(f"    {lg.address_or_area}")
            lines.append(f"    {lg.url}")
        lines.append("")
    if failed_sources:
        lines.append("Sources that failed today (skipped): " + ", ".join(failed_sources))
    return "\n".join(lines)


def _render_html_ranked(listings: list[Listing], failed_sources: list[str], esc) -> str:
    rows = []
    for i, lg in enumerate(listings, 1):   # already sorted best-first by the pipeline
        sc = _score(lg)
        badge = (f'<span style="background:#1d4ed8;color:#fff;border-radius:10px;'
                 f'padding:1px 8px;font-size:12px;font-weight:700">{sc:.0%}</span>'
                 if isinstance(sc, (int, float)) else "")
        area = f" · {lg.area_sqm:g} m²" if lg.area_sqm else ""
        rooms = f"{lg.rooms:g} Zi" if lg.rooms is not None else "? Zi"
        commute = (f'{lg.commute_minutes} min' if lg.commute_minutes is not None else "commute ?")
        price = f"€{lg.warm_price_eur:,.0f}/mo" if lg.warm_price_eur is not None else "price n/a"
        est = ' <span style="color:#b45309;font-size:12px">(est.)</span>' if lg.price_is_estimated else ""
        rows.append(f"""
          <tr><td style="padding:14px 0;border-bottom:1px solid #eee">
            <div style="color:#6b7280;font-size:13px">#{i} &nbsp; {badge}</div>
            <a href="{esc(lg.url)}" style="font-size:16px;font-weight:600;color:#1d4ed8;text-decoration:none">{esc(lg.title)}</a>
            <div style="margin-top:4px;color:#111;font-size:15px">
              <b>{price}</b> warm{est} &nbsp;·&nbsp; {esc(rooms)}{area}
              &nbsp;·&nbsp; {esc(SOURCE_LABELS.get(lg.source, lg.source))}
              &nbsp;·&nbsp; <span style="color:#047857;font-weight:600">{commute}</span>
            </div>
            <div style="margin-top:2px;color:#6b7280;font-size:13px">{esc(lg.address_or_area)}</div>
          </td></tr>""")
    footer = ""
    if failed_sources:
        footer = (f'<p style="margin-top:24px;padding:10px 12px;background:#fef2f2;'
                  f'border-radius:6px;color:#991b1b;font-size:13px">⚠️ Sources that failed today: '
                  f'{esc(", ".join(failed_sources))}</p>')
    return f"""
    <div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:640px;margin:0 auto;color:#111">
      <h1 style="font-size:20px;margin:0 0 2px">🏠 Munich furnished rentals — ranked</h1>
      <div style="color:#6b7280;font-size:14px">{date.today().isoformat()} · {len(listings)} new listing(s), best-first by predicted fit</div>
      <table width="100%" cellpadding="0" cellspacing="0">{''.join(rows)}</table>
      {footer}
      <p style="margin-top:28px;color:#9ca3af;font-size:12px">The % is the ranker's P(good).
      Prices marked "est." derive Warmmiete from Kaltmiete + assumed Nebenkosten.</p>
    </div>"""


def render_html(listings: list[Listing], failed_sources: list[str], mode: str = "by_site") -> str:
    def esc(x) -> str:
        return html.escape(str(x)) if x is not None else ""

    if mode == "ranked":
        return _render_html_ranked(listings, failed_sources, esc)

    cards = []
    for source, items in _group_by_source(listings).items():
        rows = []
        for lg in items:
            rooms = f"{lg.rooms:g} Zi" if lg.rooms is not None else "? Zi"
            area = f" · {lg.area_sqm:g} m²" if lg.area_sqm else ""
            furn = "möbliert" if lg.furnished else "furnished?"
            est = ' <span style="color:#b45309;font-size:12px">(estimated)</span>' \
                  if lg.price_is_estimated else ""
            commute = (f'<span style="color:#047857;font-weight:600">{lg.commute_minutes} min</span>'
                       if lg.commute_minutes is not None
                       else '<span style="color:#9ca3af">commute unknown</span>')
            price = f"€{lg.warm_price_eur:,.0f}/mo" if lg.warm_price_eur is not None else "price n/a"
            rows.append(f"""
              <tr><td style="padding:14px 0;border-bottom:1px solid #eee">
                <a href="{esc(lg.url)}" style="font-size:16px;font-weight:600;color:#1d4ed8;text-decoration:none">{esc(lg.title)}</a>
                <div style="margin-top:4px;color:#111;font-size:15px">
                  <b>{price}</b> warm{est}
                  &nbsp;·&nbsp; {esc(rooms)}{area}
                  &nbsp;·&nbsp; {furn}
                  &nbsp;·&nbsp; {commute}
                </div>
                <div style="margin-top:2px;color:#6b7280;font-size:13px">{esc(lg.address_or_area)}</div>
              </td></tr>""")
        cards.append(f"""
          <h2 style="font-size:15px;text-transform:uppercase;letter-spacing:.05em;color:#374151;margin:28px 0 4px">
            {esc(SOURCE_LABELS.get(source, source))} <span style="color:#9ca3af">({len(items)})</span>
          </h2>
          <table width="100%" cellpadding="0" cellspacing="0">{''.join(rows)}</table>""")

    footer = ""
    if failed_sources:
        footer = (f'<p style="margin-top:28px;padding:10px 12px;background:#fef2f2;'
                  f'border-radius:6px;color:#991b1b;font-size:13px">'
                  f'⚠️ Sources that failed today (skipped): '
                  f'{esc(", ".join(failed_sources))}</p>')

    return f"""<!-- digest -->
    <div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:640px;margin:0 auto;color:#111">
      <h1 style="font-size:20px;margin:0 0 2px">🏠 Munich furnished rentals</h1>
      <div style="color:#6b7280;font-size:14px">{date.today().isoformat()} · {len(listings)} new listing(s) {"within commute range" if _commute_known(listings) else "matching your criteria"}</div>
      {''.join(cards) if listings else '<p style="color:#6b7280">No new listings today.</p>'}
      {footer}
      <p style="margin-top:32px;color:#9ca3af;font-size:12px">Sent by your Housing Agent.
      Prices marked "estimated" derive Warmmiete from Kaltmiete + assumed Nebenkosten.</p>
    </div>"""


# ─────────────────────────────────────────────────────────────────────────────
# Sending
# ─────────────────────────────────────────────────────────────────────────────
class Emailer:
    def __init__(self, config: Config):
        self.config = config

    def build_subject(self, n: int) -> str:
        return f"{self.config.email.subject_prefix}: {n} new ({date.today().isoformat()})"

    def send(self, listings: list[Listing], failed_sources: list[str], mode: str = "by_site") -> bool:
        subject = self.build_subject(len(listings))
        text = render_plaintext(listings, failed_sources, mode)
        html_body = render_html(listings, failed_sources, mode)
        transport = self.config.email.transport
        try:
            if transport == "smtp":
                self._send_smtp(subject, text, html_body)
            elif transport == "resend":
                self._send_resend(subject, text, html_body)
            else:
                logger.error("Unknown email transport: %s (use 'resend' or 'smtp')", transport)
                return False
            logger.info("Digest sent to %s via %s", self.config.email.recipient, transport)
            return True
        except Exception as exc:
            logger.exception("Failed to send digest via %s: %s", transport, exc)
            return False

    def _from_header(self, address: str) -> str:
        return f"{self.config.email.sender_name} <{address}>"

    def _send_smtp(self, subject: str, text: str, html_body: str) -> None:
        sec = self.config.secrets
        if not sec.smtp_username or not sec.smtp_password:
            raise RuntimeError("SMTP_USERNAME/SMTP_PASSWORD not set in .env")
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self._from_header(sec.smtp_username)
        msg["To"] = self.config.email.recipient
        msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        with smtplib.SMTP(sec.smtp_host, sec.smtp_port) as server:
            server.starttls()
            server.login(sec.smtp_username, sec.smtp_password)
            server.send_message(msg)

    def _send_resend(self, subject: str, text: str, html_body: str) -> None:
        sec = self.config.secrets
        if not sec.resend_api_key:
            raise RuntimeError("RESEND_API_KEY not set in .env")
        r = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {sec.resend_api_key}"},
            json={"from": self._from_header(self.config.email.sender_address),
                  "to": [self.config.email.recipient], "subject": subject,
                  "text": text, "html": html_body},
            timeout=self.config.runtime.request_timeout_seconds,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Resend API {r.status_code}: {r.text[:300]}")
