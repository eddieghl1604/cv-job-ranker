"""
Phase 1 - check that your labels are complete, usable and consistent.

Three commands:

  1. Validate your finished labels (blanks, bad values, too few relevant CVs per job)
     python check_agreement.py validate --labels data/processed/labels.csv

  2. Make a blind re-label sheet (a random 10%, original labels hidden, re-shuffled)
     python check_agreement.py make-sheet --labels data/processed/labels.csv \
         --out data/processed/relabel.csv --frac 0.1

  3. Compare your first and second passes
     python check_agreement.py compare --labels data/processed/labels.csv \
         --relabel data/processed/relabel.csv

Label scale: 2 = strong fit, 1 = partial fit, 0 = not a fit.
"""
import argparse
from pathlib import Path

import pandas as pd

VALID = [0, 1, 2]


def load_labels(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "pair_id" not in df or "label" not in df:
        raise SystemExit(f"{path} needs 'pair_id' and 'label' columns")
    df["label"] = pd.to_numeric(df["label"], errors="coerce")
    return df


def validate(args):
    df = load_labels(args.labels)
    blank = df["label"].isna()
    bad = ~df["label"].isin(VALID) & ~blank
    print(f"Pairs: {len(df)} | blank: {blank.sum()} | invalid values: {bad.sum()}")
    if blank.any():
        print("  First blanks:", ", ".join(df.loc[blank, "pair_id"].head(10)))
    if bad.any():
        print("  Invalid:", df.loc[bad, ["pair_id", "label"]].head(10).to_string(index=False))

    done = df[~blank & ~bad]
    print("\nOverall label mix:")
    print(done["label"].value_counts().reindex(VALID, fill_value=0).to_string())

    if "job_id" in done:
        per_job = (done.groupby("job_id")["label"].value_counts()
                   .unstack(fill_value=0).reindex(columns=VALID, fill_value=0))
        per_job["relevant (1 or 2)"] = per_job[1] + per_job[2]
        print("\nPer job:")
        print(per_job.to_string())
        thin = per_job[per_job["relevant (1 or 2)"] < 5]
        if len(thin):
            print("\n! These jobs have fewer than 5 relevant CVs, so precision@5 can't reach 1.0 "
                  "even for a perfect ranker. Add more target-category CVs for them or loosen the job:")
            print("  " + ", ".join(thin.index))


def make_sheet(args):
    df = load_labels(args.labels)
    sample = df.sample(frac=args.frac, random_state=args.seed)
    cols = [c for c in ["pair_id", "job_id", "job_title", "cv_id", "cv_text"] if c in sample]
    sheet = sample[cols].copy()
    sheet["label"] = ""
    sheet.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"Wrote {len(sheet)} pairs to {args.out}. Wait at least 2-3 days, then label them "
          f"without looking at your first pass.")


def cohen_kappa(a: list, b: list):
    """
    Cohen's kappa = (p_o - p_e) / (1 - p_e)
      p_o = observed agreement: share of pairs where both passes gave the same label
      p_e = agreement you'd expect by pure chance, given how often each pass
            used each label: sum over labels k of P(pass1 = k) * P(pass2 = k)
    Kappa corrects for the fact that if you label almost everything 0,
    two passes will "agree" a lot just by luck.
    """
    n = len(a)
    p_o = sum(x == y for x, y in zip(a, b)) / n
    p_e = sum((a.count(k) / n) * (b.count(k) / n) for k in VALID)
    kappa = (p_o - p_e) / (1 - p_e) if p_e < 1 else float("nan")
    return p_o, p_e, kappa


def compare(args):
    first = load_labels(args.labels)[["pair_id", "label"]]
    second = load_labels(args.relabel)[["pair_id", "label"]]
    both = first.merge(second, on="pair_id", suffixes=("_first", "_second")).dropna()
    if both.empty:
        raise SystemExit("No overlapping labelled pairs found.")

    a = both["label_first"].astype(int).tolist()
    b = both["label_second"].astype(int).tolist()
    p_o, p_e, kappa = cohen_kappa(a, b)

    print(f"Pairs compared: {len(both)}")
    print(f"Observed agreement p_o = {p_o:.2f}")
    print(f"Chance agreement   p_e = {p_e:.2f}")
    print(f"Cohen's kappa = ({p_o:.2f} - {p_e:.2f}) / (1 - {p_e:.2f}) = {kappa:.2f}")

    print("\nConfusion table (rows = first pass, columns = second pass):")
    print(pd.crosstab(both["label_first"].astype(int), both["label_second"].astype(int),
                      rownames=["first"], colnames=["second"]).to_string())

    flips = both[both["label_first"] != both["label_second"]]
    if len(flips):
        print("\nPairs you labelled differently (re-read these and tighten your rubric):")
        print("  " + ", ".join(flips["pair_id"]))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate")
    v.add_argument("--labels", required=True, type=Path)
    v.set_defaults(func=validate)

    m = sub.add_parser("make-sheet")
    m.add_argument("--labels", required=True, type=Path)
    m.add_argument("--out", required=True, type=Path)
    m.add_argument("--frac", type=float, default=0.1)
    m.add_argument("--seed", type=int, default=7)
    m.set_defaults(func=make_sheet)

    c = sub.add_parser("compare")
    c.add_argument("--labels", required=True, type=Path)
    c.add_argument("--relabel", required=True, type=Path)
    c.set_defaults(func=compare)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
