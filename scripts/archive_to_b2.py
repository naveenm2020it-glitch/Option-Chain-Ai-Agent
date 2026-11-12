from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from storage import upload_collection


def main():
    parser = argparse.ArgumentParser(description="Store a collected NSE report batch in Backblaze B2.")
    parser.add_argument("--directory", default="data/nse_reports")
    parser.add_argument("--collection-date", default=date.today().isoformat())
    args = parser.parse_args()
    result = upload_collection(Path(args.directory), args.collection_date)
    print(f"Archived {result['report_count']} report files to {result['archive_key']}")
    print(f"SHA256: {result['archive_sha256']}")


if __name__ == "__main__":
    main()
