from datetime import date

from scripts.nse_historical_backfill import dates_between


def test_weekdays_and_official_weekend_special_session_are_included():
    days = list(dates_between(date(2026, 11, 6), date(2026, 11, 9)))
    assert days == [
        date(2026, 11, 6),
        date(2026, 11, 8),
        date(2026, 11, 9),
    ]


def test_official_muhurat_sunday_session_is_included():
    days = list(dates_between(date(2026, 11, 7), date(2026, 11, 9)))
    assert days == [date(2026, 11, 8), date(2026, 11, 9)]


def test_unlisted_sunday_is_not_included():
    days = list(dates_between(date(2026, 11, 1), date(2026, 11, 1)))
    assert days == []
