"""
Phase 1 - build the CV pool and the job-CV pairs you will hand-label.

Usage:
  python sample_pairs.py --resumes data/raw/Resume.csv --jobs data/jobs.csv --out data/processed

Inputs
  Resume.csv : Kaggle "Resume Dataset" by snehaanbhawal
               (columns: ID, Resume_str, Resume_html, Category)
  jobs.csv   : the job descriptions you wrote (see jobs_template.csv)

Outputs (written to --out)
  cvs.csv            cleaned CV pool: cv_id, category, text
  pairs_to_label.csv the sheet you label: pair_id, job_id, job_title, cv_id, cv_text, label, note
  pairs_key.csv      hidden answer key (which bucket each pair came from).
                     Do NOT open it while labelling, or you'll bias your labels.

Why three buckets per job:
  target   = CVs from the job's own category (most should be relevant)
  adjacent = CVs from neighbouring categories (the hard cases, e.g. an
             accountant CV for a financial analyst job)
  random   = CVs from unrelated categories (easy negatives)
  Without the adjacent bucket, the test is too easy: any method can tell a
  chef from a finance analyst. Ranking quality shows up on the near-misses.
"""
import argparse
import hashlib
import re
from pathlib import Path

import pandas as pd

EMAIL = re.compile(r"\S+@\S+\.\S+")
# 9-13 digits with at most one space or dash between them, so date ranges
# like "2010 - 2015" are left alone but "012-345 6789" is masked.
PHONE = re.compile(r"(?<!\d)\+?(?:\d[\s-]?){9,13}(?!\d)")


def clean(text) -> str:
    text = EMAIL.sub("[EMAIL]", str(text))
    text = PHONE.sub("[PHONE] ", text)
    return re.sub(r"\s+", " ", text).strip()


def load_cvs(path: Path, min_chars: int) -> pd.DataFrame:
    df = pd.read_csv(path)
    df = df.rename(columns={"Resume_str": "text", "Category": "category", "ID": "source_id"})
    missing = {"text", "category"} - set(df.columns)
    if missing:
        raise SystemExit(f"Resume file is missing columns: {missing}. Found: {list(df.columns)}")

    loaded = len(df)
    df["text"] = df["text"].map(clean)
    df["category"] = df["category"].astype(str).str.upper().str.strip()

    df = df[df["text"].str.len() >= min_chars].copy()
    short_dropped = loaded - len(df)

    df["hash"] = df["text"].map(lambda t: hashlib.md5(t.lower().encode()).hexdigest())
    before_dedupe = len(df)
    df = df.drop_duplicates("hash")
    dupes_dropped = before_dedupe - len(df)

    print(f"CVs loaded: {loaded} | too short: {short_dropped} | duplicates: {dupes_dropped} | kept: {len(df)}")

    df = df.reset_index(drop=True)
    df["cv_id"] = [f"cv_{i:04d}" for i in range(len(df))]
    cols = ["cv_id", "source_id", "category", "text"] if "source_id" in df else ["cv_id", "category", "text"]
    return df[cols]


def split_categories(value) -> list[str]:
    if pd.isna(value):
        return []
    return [c.strip().upper() for c in str(value).split(";") if c.strip()]


def build_pairs(cvs: pd.DataFrame, jobs: pd.DataFrame, wants: dict, seed: int) -> pd.DataFrame:
    available = set(cvs["category"])
    rows = []
    for i, job in enumerate(jobs.itertuples(index=False)):
        target = str(job.target_category).strip().upper()
        adjacent = split_categories(getattr(job, "adjacent_categories", ""))

        for cat in [target, *adjacent]:
            if cat not in available:
                print(f"  ! {job.job_id}: category '{cat}' not in the data. "
                      f"Available: {', '.join(sorted(available))}")

        pools = {
            "target": cvs[cvs["category"] == target],
            "adjacent": cvs[cvs["category"].isin(adjacent)],
            "random": cvs[~cvs["category"].isin([target, *adjacent])],
        }
        for bucket, pool in pools.items():
            k = min(wants[bucket], len(pool))
            if k < wants[bucket]:
                print(f"  ! {job.job_id}: only {k} CVs for '{bucket}' (wanted {wants[bucket]})")
            for cv in pool.sample(k, random_state=seed + i).itertuples(index=False):
                rows.append({"job_id": job.job_id, "cv_id": cv.cv_id,
                             "bucket": bucket, "category": cv.category})

    pairs = pd.DataFrame(rows)
    # Shuffle, then stable-sort by job: you label one job at a time (easier to
    # hold its criteria in your head) but the CV order inside a job is random,
    # so the bucket order can't give the answer away.
    pairs = pairs.sample(frac=1, random_state=seed).sort_values("job_id", kind="stable")
    pairs = pairs.reset_index(drop=True)
    pairs.insert(0, "pair_id", [f"p_{i:04d}" for i in range(len(pairs))])
    return pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--resumes", required=True, type=Path)
    ap.add_argument("--jobs", required=True, type=Path)
    ap.add_argument("--out", default=Path("data/processed"), type=Path)
    ap.add_argument("--n-target", type=int, default=10)
    ap.add_argument("--n-adjacent", type=int, default=8)
    ap.add_argument("--n-random", type=int, default=7)
    ap.add_argument("--min-chars", type=int, default=300)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    cvs = load_cvs(args.resumes, args.min_chars)
    jobs = pd.read_csv(args.jobs)
    for col in ("job_id", "title", "target_category"):
        if col not in jobs.columns:
            raise SystemExit(f"jobs file needs a '{col}' column")

    wants = {"target": args.n_target, "adjacent": args.n_adjacent, "random": args.n_random}
    pairs = build_pairs(cvs, jobs, wants, args.seed)

    sheet = (pairs[["pair_id", "job_id", "cv_id"]]
             .merge(jobs[["job_id", "title"]].rename(columns={"title": "job_title"}), on="job_id")
             .merge(cvs[["cv_id", "text"]].rename(columns={"text": "cv_text"}), on="cv_id"))
    sheet = sheet.set_index("pair_id").loc[pairs["pair_id"]].reset_index()
    sheet = sheet[["pair_id", "job_id", "job_title", "cv_id", "cv_text"]]
    sheet["label"] = ""
    sheet["note"] = ""

    cvs.to_csv(args.out / "cvs.csv", index=False)
    # utf-8-sig so Excel opens it without garbling characters
    sheet.to_csv(args.out / "pairs_to_label.csv", index=False, encoding="utf-8-sig")
    pairs[["pair_id", "bucket", "category"]].to_csv(args.out / "pairs_key.csv", index=False)

    print(f"\nPairs to label: {len(sheet)} across {sheet['job_id'].nunique()} jobs "
          f"({sheet['cv_id'].nunique()} distinct CVs)")
    print(pairs.groupby(["job_id", "bucket"]).size().unstack(fill_value=0).to_string())
    print(f"\nWrote {args.out}/cvs.csv, pairs_to_label.csv, pairs_key.csv")


if __name__ == "__main__":
    main()
