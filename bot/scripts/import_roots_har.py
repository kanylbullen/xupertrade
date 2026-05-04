"""Extract Roots' chart timeseries from a saved HAR file.

The chart pages on bitcoinstrategyplatform.com inline all data in a JS
script as arrays of {dateIndex: value} dicts. Letter-keys (l, o, ...)
are anti-scraping noise and ignored.

Decoding (verified empirically):
  - Constant `new Date("YYYY-MM-DD")` = chart start date
  - IS_HOURLY = false → daily data
  - Each {key: value} entry is a small batch of 1-4 consecutive days
  - Integer keys are date indices from start date
  - Values are the data point (RP, 90d change, BTC close, etc.)

Usage:
    cd bot && uv run python -m scripts.import_roots_har \\
        ../docs/www.bitcoinstrategyplatform.com.har \\
        --out-dir data/private/roots/

Reads a local HAR file only. No network, no cookies, no scraping —
just parses what your browser already fetched.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path


def _decode_response(entry: dict) -> str | None:
    content = entry["response"]["content"]
    text = content.get("text")
    if text is None:
        return None
    if content.get("encoding") == "base64":
        return base64.b64decode(text).decode("utf-8", errors="replace")
    return text


def _find_page_html(har: dict, page_url_suffix: str) -> str | None:
    for entry in har["log"]["entries"]:
        if entry["request"]["url"].endswith(page_url_suffix):
            return _decode_response(entry)
    return None


def _extract_start_date(html: str) -> datetime | None:
    """Try to find the chart's start date in the HTML. Returns None when
    not present (some pages don't expose it explicitly — caller must
    supply --start-date)."""
    m = re.search(r'new Date\("(\d{4}-\d{2}-\d{2})"\)', html)
    if m:
        return datetime.fromisoformat(m.group(1))
    return None


# Known start dates per chart page, derived from the rendered x-axis labels
# in each chart. Used as fallback when the HTML doesn't embed the anchor.
KNOWN_START_DATES: dict[str, str] = {
    "/realizedprice":             "2016-05-04",
    "/sth-costbasis":             "2010-07-17",
    "/sth-costbasis-trendline":   "2010-07-17",
    "/lth-costbasis":             "2012-08-01",  # aligned so max-key = today's date
    "/mvrv":                      "2012-05-03",  # aligned so max-key = today's date
    "/cvdd":                      "2010-07-17",
}


def _extract_big_arrays(html: str, min_chars: int = 5000) -> list[str]:
    """Find arrays of {...},{...},... bigger than min_chars characters."""
    arrays: list[str] = []
    for match_idx in [m.start() for m in re.finditer(r'=\s*\[\{', html)]:
        bracket_start = html.find('[', match_idx)
        if bracket_start < 0:
            continue
        depth = 0
        end = None
        for i in range(bracket_start, len(html)):
            ch = html[i]
            if ch == '[':
                depth += 1
            elif ch == ']':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end and (end - bracket_start) >= min_chars:
            arrays.append(html[bracket_start:end])
    return arrays


def _parse_array_to_indexed(raw: str) -> dict[int, float]:
    """Parse [{idx:val, idx:val, ...}, ...] into a flat {date_index: value} map.

    Letter keys (l, o, ...) are anti-scraping noise — ignored.
    On collision, the LAST occurrence wins (latest entry in source).
    """
    out: dict[int, float] = {}
    # Match key:value pairs where key is digits and value is a number or null
    # We accept keys that are pure digits — letter keys are deliberately skipped.
    pair_re = re.compile(
        r'(\d+)\s*:\s*(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|null)'
    )
    for key_str, val_str in pair_re.findall(raw):
        if val_str == "null":
            continue
        idx = int(key_str)
        out[idx] = float(val_str)
    return out


def _classify(values_by_idx: dict[int, float], n_total_days: int) -> str:
    """Heuristic name for an array based on value range + null density."""
    if not values_by_idx:
        return "empty"
    nums = list(values_by_idx.values())
    avg = sum(abs(v) for v in nums) / len(nums)
    coverage = len(values_by_idx) / max(n_total_days, 1)

    # 90d change: percentages, almost always |v| < 5
    if max(abs(v) for v in nums) < 5 and avg < 1:
        return "rp_90d_change"
    # ATH markers: very sparse coverage (mostly null) but real prices when set
    if coverage < 0.1:
        return "ath_markers"
    # BTC close vs Realized Price — both span $1 to $130k
    # RP changes monotonically; BTC is volatile. We'd need adjacency to tell.
    # For now lump them as "price_series" and let the user inspect output.
    return "price_series"


def _today_index(start: datetime) -> int:
    today = datetime.utcnow()
    return (today - start).days


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("har", help="HAR file to parse")
    parser.add_argument("--page", default="/realizedprice",
                        help="URL suffix to find the chart page")
    parser.add_argument("--out-dir", default="data/private/roots/",
                        help="Where to write CSVs")
    parser.add_argument("--start-date", default=None,
                        help="ISO start date (overrides auto-detect / KNOWN_START_DATES)")
    parser.add_argument("--list-only", action="store_true",
                        help="Just describe what's in the HAR; don't write CSVs")
    args = parser.parse_args()

    har = json.load(open(args.har))
    html = _find_page_html(har, args.page)
    if html is None:
        print(f"No HAR entry matches page suffix {args.page!r}", file=sys.stderr)
        return 2

    start = None
    if args.start_date:
        start = datetime.fromisoformat(args.start_date)
    else:
        start = _extract_start_date(html)
        if start is None and args.page in KNOWN_START_DATES:
            start = datetime.fromisoformat(KNOWN_START_DATES[args.page])
            print(f"(no anchor in HTML — using KNOWN_START_DATES[{args.page!r}])")
    if start is None:
        print(
            f"error: no start date in HTML and no fallback for {args.page!r}; "
            f"pass --start-date YYYY-MM-DD",
            file=sys.stderr,
        )
        return 2

    today_idx = _today_index(start)
    print(f"chart start date: {start.date()}, today is index {today_idx}")

    arrays = _extract_big_arrays(html)
    print(f"found {len(arrays)} large data arrays")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    parsed = []
    for i, raw in enumerate(arrays):
        by_idx = _parse_array_to_indexed(raw)
        kind = _classify(by_idx, today_idx + 1)
        if not by_idx:
            print(f"  array {i}: empty")
            continue
        sorted_idx = sorted(by_idx)
        first_idx, last_idx = sorted_idx[0], sorted_idx[-1]
        first_date = (start + timedelta(days=first_idx)).date()
        last_date = (start + timedelta(days=last_idx)).date()
        print(
            f"  array {i}: {len(by_idx)} points  "
            f"{first_date} → {last_date}  "
            f"vals {min(by_idx.values()):,.4f} → {max(by_idx.values()):,.4f}  "
            f"kind={kind}"
        )
        parsed.append((i, by_idx, kind))

    if args.list_only:
        return 0

    for i, by_idx, kind in parsed:
        if kind == "empty":
            continue
        out_path = out_dir / f"array_{i:02d}_{kind}.csv"
        with open(out_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "value"])
            for idx in sorted(by_idx):
                date = start + timedelta(days=idx)
                w.writerow([date.date().isoformat(), f"{by_idx[idx]:.8f}"])
        print(f"  wrote {out_path}  ({len(by_idx)} rows)")

    print(
        f"\nDone. Inspect a few CSV rows against the chart on the website to "
        f"confirm which array corresponds to which series, then rename the "
        f"file (e.g. array_03_rp_90d_change.csv → rp_90d_change.csv)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
