from nifty_ai.ingest import _date_from_filename


def test_participant_volume_filename_date():
    assert _date_from_filename("fao_participant_vol_11112026.csv") == "2026-11-11"


def test_fii_stats_filename_date():
    assert _date_from_filename("fii_stats_11-Nov-2026.xls") == "2026-11-11"


def test_invalid_report_filename_has_no_invented_date():
    assert _date_from_filename("fii_stats_32-Nov-2026.xls") is None
