"""Unit tests for ingestion helpers that are easy to get subtly wrong. No network access."""
from datetime import date

import pytest

from src.ingest import fred
from src.ingest.cleveland_nowcast import label_to_date, parse
from src.ingest.kalshi import is_combo_series, volume_of
from src.ingest.validate import to_num


# ---------------------------------------------------------------- Cleveland Fed label dates

def test_label_same_year():
    assert label_to_date("07/15", 2026, 7) == date(2026, 7, 15)
    assert label_to_date("08/12", 2026, 7) == date(2026, 8, 12)


def test_label_wraps_into_next_year():
    # Dec 2024 target: nowcasting continues into January 2025
    assert label_to_date("01/10", 2024, 12) == date(2025, 1, 10)


def test_long_shutdown_month_does_not_wrap():
    # Oct 2025 target ran to mid-December 2025, same year
    assert label_to_date("12/15", 2025, 10) == date(2025, 12, 15)


def test_parse_skips_vline_markers_and_keeps_alignment():
    chart = {
        "chart": {"subcaption": "2026-7"},
        "categories": [{"category": [{"label": "08/07"}, {"label": "CPI Jun", "vline": "true"},
                                     {"label": "08/10"}]}],
        "dataset": [{"seriesname": "CPI Inflation",
                     "data": [{"value": "0.1"}, {"value": "0.2"}, {"value": ""}]}],
    }
    rows, markers = parse([chart])
    assert rows == [{"reference_month": "2026-07", "nowcast_date": "2026-08-07",
                     "measure": "CPI", "kind": "nowcast", "value": 0.1}]
    assert markers == [{"reference_month": "2026-07", "marker": "CPI Jun", "after_date": "2026-08-07"}]


# ---------------------------------------------------------------- ALFRED first release

@pytest.mark.parametrize("x, expected", [(0.05, 0.1), (0.0499, 0.0), (-0.05, -0.1), (1.322, 1.3), (0.287, 0.3)])
def test_round_half_up_one_decimal(x, expected):
    assert fred.round_1dp(x) == expected


def test_month_add_crosses_year():
    assert fred.month_add(date(2025, 11, 1), 2) == date(2026, 1, 1)
    assert fred.month_add(date(2026, 1, 1), -1) == date(2025, 12, 1)


def test_release_date_binary_search(monkeypatch):
    # Fake ALFRED: June 2026 appears in vintages from 2026-07-14 onward.
    release = date(2026, 7, 14)
    month = date(2026, 6, 1)
    monkeypatch.setattr(fred, "has_month", lambda series, vdate, m: vdate >= release)
    assert fred.find_release_date("CPIAUCSL", month) == release


def test_release_date_none_when_never_published(monkeypatch):
    monkeypatch.setattr(fred, "has_month", lambda series, vdate, m: False)
    assert fred.find_release_date("CPIAUCSL", date(2025, 10, 1)) is None


# ---------------------------------------------------------------- Kalshi field handling

def test_volume_field_names_differ_by_tier():
    assert volume_of({"volume_fp": "12.50"}) == 12.5
    assert volume_of({"volume": 7}) == 7.0
    assert volume_of({}) == 0.0


def test_combo_series_excluded():
    assert is_combo_series({"ticker": "KXMVECROSSCATEGORY", "tags": []})
    assert not is_combo_series({"ticker": "KXCPI", "tags": ["Inflation"]})


@pytest.mark.parametrize("raw, expected", [(".9%", 0.9), ("0.1", 0.1), ("-0.2", -0.2), ("", None), (None, None)])
def test_kalshi_expiration_value_parsing(raw, expected):
    assert to_num(raw) == expected
