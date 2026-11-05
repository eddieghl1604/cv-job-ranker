"""
Read your finished labelling workbook and save the labels WITHOUT any CV text,
so the result is safe to commit to GitHub.

Usage:
  python scripts/export_labels.py --workbook data/processed/labelling.xlsx --out data/labels.csv

Output columns: pair_id, job_id, cv_id, MH1, MH2, MH3, suggested, label, note

It also reports:
  - rows with no label yet
  - rows where your label differs from what the rubric suggests (overrides).
    A few are fine; lots of them mean the rubric doesn't match how you
    actually judge, and you should fix the rubric rather than keep overriding.
"""
import argparse
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

HEADER_ROW = 7


def suggest(marks: list[str]):
    marks = [m for m in marks if m in ("Y", "P", "N")]
    n = len(marks)
    if n == 0:
        return None
    y, no = marks.count("Y"), marks.count("N")
    if y == n:
        return 2
    if y >= n - 1 or no == 0:
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workbook", required=True, type=Path)
    ap.add_argument("--out", default=Path("data/labels.csv"), type=Path)
    args = ap.parse_args()

    wb = load_workbook(args.workbook)
    records = []
    for ws in wb.worksheets:
        headers = [c.value for c in ws[HEADER_ROW]]
        if "pair_id" not in headers:
            continue
        mh_cols = [h for h in headers if h and str(h).startswith("MH")]
        for row in ws.iter_rows(min_row=HEADER_ROW + 1, values_only=True):
            rec = dict(zip(headers, row))
            if not rec.get("pair_id"):
                continue
            marks = [str(rec.get(h) or "").strip().upper() for h in mh_cols]
            out = {"pair_id": rec["pair_id"], "job_id": ws.title, "cv_id": rec["cv_id"]}
            out.update({h: m for h, m in zip(mh_cols, marks)})
            out["suggested"] = suggest(marks) if all(marks) else None
            label = rec.get("label")
            out["label"] = int(label) if label not in (None, "") else None
            out["note"] = rec.get("note") or ""
            records.append(out)

    df = pd.DataFrame(records)
    for c in ("suggested", "label"):
        df[c] = df[c].astype("Int64")  # whole numbers, blanks allowed
    args.out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.out, index=False)

    missing = df["label"].isna().sum()
    done = df.dropna(subset=["label", "suggested"])
    overrides = done[done["label"] != done["suggested"]]
    print(f"Exported {len(df)} rows to {args.out} | labelled: {len(df) - missing} | unlabelled: {missing}")
    print(f"Overrides (label differs from rubric suggestion): {len(overrides)}")
    if len(overrides):
        print(overrides[["pair_id", "job_id", "suggested", "label", "note"]].to_string(index=False))
        no_note = overrides[overrides["note"].astype(str).str.strip() == ""]
        if len(no_note):
            print(f"\n! {len(no_note)} override(s) have no note. Add a reason for each.")


if __name__ == "__main__":
    main()
