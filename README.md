# 🏠 Munich Housing Agent

Runs once per day, searches furnished-rental sites for **furnished 1-bedroom
apartments in/near Munich**, keeps only the ones **within 40 minutes by public
transport** of your office, and **emails you a digest of new listings** — with
prices, commute times, and direct links. It only ever emails listings you
haven't already been sent.

## What it does

1. **Scrapes** furnished-rental sites (see [Sources](#sources)).
2. **Filters** to Warmmiete ≤ €1,500, 1–2 Zimmer, furnished, available from your
   lease-start date, excluding student-only and shared/WG rooms.
3. **Computes public-transport commute** (door-to-door, weekday 09:00 arrival) to
   your office and keeps only ≤ 40 min.
4. **De-duplicates** against everything it has sent before (SQLite).
5. **Emails** a clean HTML + plaintext digest grouped by source.

All search parameters live in [`config.yaml`](config.yaml); secrets live in `.env`.

---

## Quick start

```bash
# 1. Install dependencies (Python 3.11+)
python -m pip install -r requirements.txt

# 2. Configure secrets
cp .env.example .env        # then edit .env (see below)

# 3. (optional) tweak search settings in config.yaml

# 4. Try it without sending email — writes data/digest_preview.html + .txt
python -m housing_agent --dry-run

# 5. When happy, run for real
python -m housing_agent
```

Useful commands:

| Command | What it does |
|---|---|
| `python -m housing_agent` | Full run: scrape → filter → commute → dedup → **email** |
| `python -m housing_agent --dry-run` | Same, but render the digest to `data/digest_preview.{html,txt}` instead of sending |
| `python -m housing_agent --scrape-only` | Just scrape and print normalized listings (no filtering/email) |
| `python -m housing_agent --scrape-only --json` | Same, as JSON |
| `python -m housing_agent --check-commute` | Print commute times to known Munich points — verifies your transit provider |
| `python -m housing_agent --test-email` | Send a small sample digest to confirm your email setup |

---

## Configuration

### `.env` (secrets — never commit this)

| Variable | Needed for | Notes |
|---|---|---|
| `GOOGLE_MAPS_API_KEY` | Commute + geocoding | One key, "Routes API" + "Geocoding API" enabled. See [below](#getting-a-google-maps-api-key) |
| `RESEND_API_KEY` | Sending email (default) | From [resend.com](https://resend.com) |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | Email if you use SMTP instead | Gmail address + **app password** |
| `SMTP_HOST` / `SMTP_PORT` | SMTP | Default `smtp.gmail.com` / `587` |

### `config.yaml` (behaviour — safe to edit)

The important knobs (full comments in the file):

- `search.anchor_address` / `anchor_lat` / `anchor_lng` — your office (the commute
  anchor). Coordinates are pre-filled for Kaulbachstraße 4.
- `search.max_commute_minutes` — default `40`.
- `search.max_price_eur` — Warmmiete cap, default `1500`.
- `search.min_rooms` / `max_rooms` — `1`–`2` Zimmer (German counting: 1-Zi = studio,
  2-Zi = 1 bedroom + living room).
- `search.move_in_date` / `earliest_move_in` — lease start; the agent queries
  availability from the earliest acceptable date.
- `search.nebenkosten_estimate_eur` — when a listing only shows Kaltmiete, warm is
  estimated as Kalt + this (default `250`) and flagged **(estimated)** in the email.
- `search.city` / `country` — the target city (see [Targeting another city](#targeting-another-city)).
- `sources.*.enabled` — toggle each site on/off.
- `email.transport` / `recipient` — `resend` (default) or `smtp`.

---

## Getting a Google Maps API key

The default commute provider uses Google's **Routes API** (the modern replacement
for the now-legacy Directions API) and the **Geocoding API**.

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) → create (or
   pick) a project.
2. **APIs & Services → Enable APIs & Services**, and enable **both**:
   - **Routes API**  ← for transit times (⚠️ *not* the old "Directions API", which
     Google no longer activates for new projects)
   - **Geocoding API**  ← to locate listings that don't ship coordinates
3. **APIs & Services → Credentials → Create credentials → API key**. Copy it into
   `GOOGLE_MAPS_API_KEY` in `.env`.
4. (Recommended) Restrict the key to those two APIs.
5. Verify: `python -m housing_agent --check-commute` should print realistic times
   (e.g. Marienplatz ~9 min, Bad Tölz ~100 min).

**Cost:** the free monthly credit easily covers a daily run — results are cached
(`commute.cache_ttl_days`), so repeat listings cost nothing.

---

## Email

Default transport is **Resend**: put `RESEND_API_KEY` in `.env` and run
`python -m housing_agent --test-email`. Without a verified domain, Resend's shared
sender (`onboarding@resend.dev`) only delivers to the address you registered your
Resend account with — verify a domain in Resend to send anywhere.

To use **Gmail SMTP** instead, set `email.transport: smtp` in `config.yaml`, enable
2-Step Verification, create an app password at
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords), and
put your Gmail address + app password in `SMTP_USERNAME` / `SMTP_PASSWORD`.

---

## Sources

All sources use **plain HTTP** — no browser, no metered APIs — preferring embedded
structured data or server-rendered cards, which is faster and more stable than DOM
scraping and needs nothing to install.

| Site | Method | Notes |
|---|---|---|
| **Wunderflats** | Listings JSON embedded in the page | Coords included → no geocoding |
| **HousingAnywhere** | Server-rendered cards | Geocoded; counts bedrooms → mapped to Zimmer |
| **Spacest** | `__NEXT_DATA__` JSON | Coords included; furnished mid-term (warm) |
| **WG-Gesucht** | Server-rendered cards | 1-Zimmer möbliert category; Kaltmiete; geocoded |
| **Kleinanzeigen** | Server-rendered cards | Kaltmiete; keeps only "möbliert" listings; geocoded |
| **Mr. Lodge** | Server-rendered cards | Munich furnished agency (premium); warm; geocoded |
| **Immowelt** | Server-rendered cards | ⚠️ DataDome-protected → **disabled by default**; fails gracefully if challenged |

> **Evaluated and dropped:** **ImmobilienScout24** and **Immonet** (DataDome — an
> "Ich bin kein Roboter" 403 to plain HTTP, headless browsers, and even Browser Use
> cloud via a German residential proxy); **Amber** (client-only SPA, and it's
> student housing); **Nestpick** (an aggregator that redirects to HousingAnywhere
> etc., so it would just duplicate sources we already scrape).

Kaltmiete-only sources (WG-Gesucht, Kleinanzeigen, Immowelt) have their Warmmiete
estimated (Kalt + Nebenkosten) and flagged **(est.)** in the digest. Cities are set
in `config.yaml`; note that WG-Gesucht and Kleinanzeigen key cities by numeric ID,
so a new city needs one entry added to the `CITY_IDS` / `CITY_CODES` map in those
scrapers.

### Adding another site

Subclass `BaseScraper` in `housing_agent/scrapers/`, return normalized `Listing`
objects, and register it in `scrapers/__init__.py` under the key you use in
`config.yaml`. Selectors/URLs are heavily commented because they're what breaks
when sites change. One scraper failing never affects the others or the email.

---

## Targeting another city

The agent isn't Munich-specific. To point it at another city, edit **four values**
in `config.yaml` and nothing else:

```yaml
search:
  city: "berlin"                       # drives the search URLs
  country: "Germany"                   # used by HousingAnywhere
  anchor_address: "Your office, Berlin"  # commute is measured to here
  anchor_lat: 52.5200                  # (optional but exact/cheaper than geocoding)
  anchor_lng: 13.4050
```

Prices/rooms/dates and all other filters are already parameters. Everything
downstream (scrapers, geocoding, commute, filters, email) reads from these values.

---

## Scheduling

### Option A — GitHub Actions (runs in the cloud, no machine needed)

The workflow is at [`.github/workflows/daily.yml`](.github/workflows/daily.yml)
(daily at ~12:00 Europe/Berlin). Set repository **Secrets** (Settings → Secrets and
variables → Actions): `GOOGLE_MAPS_API_KEY` and `RESEND_API_KEY` (or the `SMTP_*`
vars if you use SMTP). Trigger a manual run from the Actions tab to test.

**Dedup state** is persisted reliably on a dedicated `agent-state` branch: the
workflow restores `seen.json` from it before each run and force-pushes the updated
copy after. `main` stays clean (no automated commits), so your own pushes never
conflict. The very first run after enabling this sends the full digest once (empty
state), then every later run emails only genuinely new listings.

### Option B — cron on a local machine / VPS

```cron
# 08:00 Europe/Berlin daily. Adjust the path and set TZ appropriately.
0 8 * * *  cd /path/to/housing_agent && /usr/bin/python3 -m housing_agent >> data/cron.log 2>&1
```

On a VPS the `data/` directory persists on disk, so dedup is exact.

### Option C — Windows Task Scheduler

Create a Basic Task → Daily → 08:00 → *Start a program*:
`python` with arguments `-m housing_agent`, "Start in" set to the project folder.

---

## How it handles German rental nuances

- **Warmmiete vs Kaltmiete** — filtering is always on the *warm* (all-in) price. If
  a listing shows only Kaltmiete, warm is estimated as Kalt + Nebenkosten and
  flagged **(estimated)**.
- **Zimmer counting** — 1-Zimmer = studio, 2-Zimmer = 1 bedroom + living room. The
  target "1-bedroom" maps to **1–2 Zimmer**. HousingAnywhere counts *bedrooms*, so
  its "Studio" → 1 Zi and "1 bedroom" → 2 Zi to stay consistent.
- **möbliert** — only furnished listings are kept.

---

## Resilience

- One scraper failing never stops the others or the email — failed sources are
  listed in the digest footer.
- If the commute provider is unavailable, listings are kept and flagged "commute
  unknown" rather than silently dropped.
- Listings are recorded as "sent" **only after** a successful email, so a failed
  send won't cause you to miss them next time.
- Structured logs go to stdout and `data/housing_agent.log`.

## Tests

```bash
python tests/test_commute.py     # commute filter + arrival-time logic
python tests/test_pipeline.py    # filters, dedup store, SMTP send (fake server)
# or, if you have pytest: python -m pytest -q
```

## Project layout

```
housing_agent/
├── __main__.py       # CLI entrypoint
├── config.py         # config.yaml + .env → typed dataclasses
├── models.py         # normalized Listing
├── pipeline.py       # scrape → filter → commute → dedup → email
├── filters.py        # warm price, rooms, furnished, student/shared exclusions
├── commute.py        # transit time (Google Routes API) + cache
├── geocode.py        # address → coords (Google Geocoding) + cache
├── store.py          # SQLite dedup
├── emailer.py        # HTML + plaintext digest, Resend/SMTP
├── cache.py          # tiny JSON TTL cache
└── scrapers/         # one module per site
```
