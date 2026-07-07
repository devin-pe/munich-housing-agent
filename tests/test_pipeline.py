"""Tests for dedup store, filters, and the SMTP send path (fake server)."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from housing_agent.config import load_config
from housing_agent.emailer import Emailer, render_html
from housing_agent.filters import apply_filters, compute_warm_price
from housing_agent.models import Listing
from housing_agent.store import SeenStore


def _lst(idx, price=1200, ptype="warm", rooms=1, furnished=True, only_students=False):
    return Listing(
        source="wunderflats", url=f"http://x/{idx}", listing_id=str(idx),
        title=f"Flat {idx}", price_eur=price, price_type=ptype, rooms=rooms,
        furnished=furnished, address_or_area="München", lat=48.14, lng=11.57,
        extra={"only_students": only_students},
    )


def test_warm_price_estimation():
    lg = _lst(1, price=1200, ptype="kalt")
    compute_warm_price(lg, nebenkosten_estimate=250)
    assert lg.warm_price_eur == 1450 and lg.price_is_estimated is True
    lg2 = _lst(2, price=1400, ptype="warm")
    compute_warm_price(lg2, nebenkosten_estimate=250)
    assert lg2.warm_price_eur == 1400 and lg2.price_is_estimated is False


def test_apply_filters():
    cfg = load_config()
    cfg.search.max_price_eur = 1500
    cfg.search.min_rooms, cfg.search.max_rooms = 1, 2
    listings = [
        _lst(1, price=1200),                       # keep
        _lst(2, price=1600),                       # drop: price
        _lst(3, price=1300, ptype="kalt"),         # 1300+250=1550 -> drop: price
        _lst(4, price=1000, rooms=3),              # drop: rooms
        _lst(5, price=1000, only_students=True),   # drop: student-only
    ]
    kept, stats = apply_filters(listings, cfg)
    ids = {lg.listing_id for lg in kept}
    assert ids == {"1"}, ids
    assert stats["dropped_price"] == 2
    assert stats["dropped_rooms"] == 1
    assert stats["dropped_student_only"] == 1


def test_seen_store_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        store = SeenStore(d)
        listings = [_lst(1), _lst(2)]
        assert len(store.filter_new(listings)) == 2      # both new
        store.mark_sent(listings)
        assert store.filter_new(listings) == []          # now seen
        assert len(store.filter_new([_lst(1), _lst(3)])) == 1  # only #3 new
        store.close()


def test_smtp_send_with_fake_server(monkeypatch=None):
    cfg = load_config()
    cfg.email.transport = "smtp"
    cfg.secrets.smtp_username = "me@gmail.com"
    cfg.secrets.smtp_password = "app-password"

    sent = {}

    class FakeSMTP:
        def __init__(self, host, port): sent["host"] = host; sent["port"] = port
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self): sent["tls"] = True
        def login(self, u, p): sent["login"] = (u, p)
        def send_message(self, msg): sent["msg"] = msg

    import housing_agent.emailer as emailer_mod
    emailer_mod.smtplib.SMTP = FakeSMTP  # type: ignore

    ok = Emailer(cfg).send([_lst(1), _lst(2)], failed_sources=["some-source"])
    assert ok is True
    assert sent["tls"] is True
    assert sent["login"] == ("me@gmail.com", "app-password")
    assert sent["msg"]["To"] == cfg.email.recipient
    assert "Flat 1" in sent["msg"].as_string() or "Flat 1" in render_html([_lst(1)], [])


if __name__ == "__main__":
    test_warm_price_estimation()
    test_apply_filters()
    test_seen_store_roundtrip()
    test_smtp_send_with_fake_server()
    print("OK: all pipeline tests passed")
