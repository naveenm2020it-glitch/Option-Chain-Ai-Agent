"""Rebuild the checked-in source archive from GitHub's binary bundle parts."""
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
PARTS = ROOT / "bundle.parts"
ARCHIVE = ROOT / "nifty-ai-source.zip"

def main():
    # Prefer reviewed, version-controlled application files over the legacy archive.
    if (ROOT / "app.py").is_file() and (ROOT / "nifty_ai" / "analytics.py").is_file():
        print("Using checked-in application source; legacy bundle extraction skipped.")
        return
    pieces = sorted(PARTS.glob("part-*"))
    if not pieces:
        raise SystemExit("Source bundle parts were not found in bundle.parts/.")
    with ARCHIVE.open("wb") as target:
        for piece in pieces:
            with piece.open("rb") as source:
                while block := source.read(1024 * 1024):
                    target.write(block)
    with zipfile.ZipFile(ARCHIVE) as archive:
        bad = archive.testzip()
        if bad:
            raise SystemExit(f"Source archive validation failed at {bad}.")
        archive.extractall(ROOT)
    print(f"Source archive reconstructed and extracted: {ARCHIVE}")

if __name__ == "__main__":
    main()
