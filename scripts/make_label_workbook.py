"""
Build an Excel workbook for labelling: one sheet per job, the job's must-haves
shown at the top, and a Y/P/N box for each must-have on every CV.

Usage:
  python scripts/make_label_workbook.py --pairs data/processed/pairs_to_label.csv \
      --jobs data/jobs.csv --out data/processed/labelling.xlsx

How you use it:
  For each CV, mark every must-have Y (met), P (partly met) or N (not met).
  The 'suggested' column then applies the rubric for you:
      2 if every must-have is Y
      1 if all but one are Y, or none are N
      0 otherwise
  Type your final score in 'label' (normally the same as suggested).
  If you overrule it, say why in 'note'.

Why tick must-haves instead of typing a score straight away:
  it forces you to check each criterion the same way every time, and the
  Y/P/N marks become extra test data for Phase 2 (did the AI extract the
  same facts you saw?).
"""
import argparse
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

HEADER_ROW = 7
WRAP_TOP = Alignment(wrap_text=True, vertical="top")
INPUT_FILL = PatternFill("solid", fgColor="FFF2CC")  # pale yellow = cells you fill in


def split_must_haves(text) -> list[str]:
    return [m.strip() for m in str(text).split(";") if m.strip()]


def build_sheet(wb, job, rows: pd.DataFrame):
    ws = wb.create_sheet(job.job_id)
    mhs = split_must_haves(job.must_haves)
    n = len(mhs)

    ws["A1"] = f"{job.job_id}: {job.title}"
    ws["A1"].font = Font(bold=True, size=14)
    for i, mh in enumerate(mhs[:4], start=1):  # rows 2-5
        ws.cell(row=1 + i, column=1, value=f"MH{i}: {mh}").font = Font(bold=True)
    ws.cell(row=HEADER_ROW - 1, column=1,
            value="Mark each must-have Y / P / N. 'suggested' applies the rubric. "
                  "Type your final 0/1/2 in 'label'; explain any override in 'note'.").font = Font(italic=True)

    headers = ["pair_id", "cv_id", *[f"MH{i}" for i in range(1, n + 1)],
               "suggested", "label", "note", "cv_text"]
    for c, h in enumerate(headers, start=1):
        cell = ws.cell(row=HEADER_ROW, column=c, value=h)
        cell.font = Font(bold=True)

    col = {h: i + 1 for i, h in enumerate(headers)}
    mh_first = get_column_letter(col["MH1"])
    mh_last = get_column_letter(col[f"MH{n}"])

    dv_mh = DataValidation(type="list", formula1='"Y,P,N"', allow_blank=True)
    dv_label = DataValidation(type="list", formula1='"0,1,2"', allow_blank=True)
    ws.add_data_validation(dv_mh)
    ws.add_data_validation(dv_label)

    for r, row in enumerate(rows.itertuples(index=False), start=HEADER_ROW + 1):
        ws.cell(row=r, column=col["pair_id"], value=row.pair_id)
        ws.cell(row=r, column=col["cv_id"], value=row.cv_id)
        rng = f"{mh_first}{r}:{mh_last}{r}"
        ws.cell(row=r, column=col["suggested"], value=(
            f'=IF(COUNTA({rng})<{n},"",'
            f'IF(COUNTIF({rng},"Y")={n},2,'
            f'IF(OR(COUNTIF({rng},"Y")>={n - 1},COUNTIF({rng},"N")=0),1,0)))'))
        ws.cell(row=r, column=col["cv_text"], value=row.cv_text).alignment = WRAP_TOP
        ws.cell(row=r, column=col["note"]).alignment = WRAP_TOP
        for h in [*[f"MH{i}" for i in range(1, n + 1)], "label", "note"]:
            ws.cell(row=r, column=col[h]).fill = INPUT_FILL
        for i in range(1, n + 1):
            dv_mh.add(ws.cell(row=r, column=col[f"MH{i}"]))
        dv_label.add(ws.cell(row=r, column=col["label"]))
        ws.row_dimensions[r].height = 409  # Excel's maximum row height
        for c in range(1, len(headers)):
            ws.cell(row=r, column=c).alignment = Alignment(vertical="top", wrap_text=True)

    widths = {"pair_id": 9, "cv_id": 9, "suggested": 10, "label": 7, "note": 28, "cv_text": 120}
    for h, c in col.items():
        ws.column_dimensions[get_column_letter(c)].width = widths.get(h, 6)
    ws.freeze_panes = ws.cell(row=HEADER_ROW + 1, column=1)
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", required=True, type=Path)
    ap.add_argument("--jobs", required=True, type=Path)
    ap.add_argument("--out", default=Path("data/processed/labelling.xlsx"), type=Path)
    args = ap.parse_args()

    if args.out.exists():
        raise SystemExit(f"{args.out} already exists. It may contain your labels, so I won't overwrite it. "
                         f"Rename or delete it yourself if you really want a fresh one.")

    pairs = pd.read_csv(args.pairs)
    jobs = pd.read_csv(args.jobs)
    wb = Workbook()
    wb.remove(wb.active)
    for job in jobs.itertuples(index=False):
        rows = pairs[pairs["job_id"] == job.job_id]
        n = build_sheet(wb, job, rows)
        if n != 3:
            print(f"  note: {job.job_id} has {n} must-haves (the rubric assumes 3)")
        print(f"{job.job_id}: {len(rows)} CVs, {n} must-haves")
    wb.save(args.out)
    print(f"\nSaved {args.out}")


if __name__ == "__main__":
    main()
