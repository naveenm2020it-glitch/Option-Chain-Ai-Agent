import io
import zipfile

import pytest

from scripts.nse_download_check import validate_payload


def make_zip(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


def test_zip_validator_ignores_empty_directory_entries():
    payload = make_zip([
        ("fo27102025/", b""),
        ("fo27102025/fo27102025.csv", b"date,value\n2025-10-27,123\n"),
    ])
    result = validate_payload("fo_market_activity", payload, "zip")
    assert result["nonempty_member_count"] == 1
    assert result["uncompressed_nonempty_bytes"] > 10


def test_zip_validator_rejects_archive_with_only_empty_files():
    payload = make_zip([
        ("empty.csv", b""),
        ("also-empty.csv", b""),
    ])
    with pytest.raises(ValueError, match="no non-empty data files"):
        validate_payload("fo_market_activity", payload, "zip")
