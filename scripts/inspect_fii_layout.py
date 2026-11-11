from pathlib import Path
import pandas as pd

files = sorted(Path("data/nse_reports/fii_derivatives").glob("*.xls"))
if not files:
    raise SystemExit("No archived FII XLS files found")
path = files[-1]
print("FILE:", path)
for header in (None, 0, 1, 2, [1, 2]):
    try:
        book = pd.read_excel(path, sheet_name=None, engine="xlrd", header=header)
        print("\nHEADER:", header)
        for name, frame in book.items():
            print("SHEET:", name, "SHAPE:", frame.shape)
            print("COLUMNS:", [str(x) for x in frame.columns])
            print(frame.head(12).to_string(index=False, header=True))
    except Exception as exc:
        print("HEADER FAILED:", header, repr(exc))

# Trigger diagnostic workflow after it has been added.
