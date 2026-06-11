"""Fetch free M1 OHLC from HistData.com (Generic ASCII) for the portfolio test.

HistData serves one ZIP per (pair, year): a token is scraped from the pair-year
page, then POSTed to get.php. Format inside: `YYYYMMDD HHMMSS;O;H;L;C;V` in EST
(constant UTC-5, no DST), volume always 0. We shift +5h to approximate UTC
(the ~1h DST misalignment is immaterial for an H4 strategy).

    python3 scripts/fetch_histdata_m1.py --pair USDJPY --start 2016 --end 2024

Output: data/candles/histdata/<PAIR>_M1.csv  (combined, time/open/high/low/close)
Resumable: per-year ZIPs cached under data/candles/histdata/_raw/.
"""

from __future__ import annotations

# Deliberate fixed-host (https://www.histdata.com) fetcher — S310 audit n/a.
# ruff: noqa: S310
import argparse
import io
import re
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "candles" / "histdata"
RAW = OUT / "_raw"
UA = {"User-Agent": "Mozilla/5.0"}


def fetch_year(pair: str, year: int) -> bytes:
    cache = RAW / f"{pair}_{year}.zip"
    if cache.exists():
        return cache.read_bytes()
    page = (
        f"https://www.histdata.com/download-free-forex-historical-data/"
        f"?/ascii/1-minute-bar-quotes/{pair.lower()}/{year}"
    )
    req = urllib.request.Request(page, headers=UA)
    html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
    m = re.search(r'id="tk"\s+value="([^"]+)"', html)
    if not m:
        raise RuntimeError(f"no download token on page for {pair} {year}")
    tk = m.group(1)
    form = {
        "tk": tk,
        "date": str(year),
        "datemonth": str(year),
        "platform": "ASCII",
        "timeframe": "M1",
        "fxpair": pair.upper(),
    }
    hdr = {**UA, "Referer": page, "Content-Type": "application/x-www-form-urlencoded"}
    post = urllib.request.Request(
        "https://www.histdata.com/get.php", data=urllib.parse.urlencode(form).encode(), headers=hdr
    )
    resp = urllib.request.urlopen(post, timeout=90).read()
    RAW.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(resp)
    return resp


def parse_year(raw: bytes) -> pd.DataFrame:
    z = zipfile.ZipFile(io.BytesIO(raw))
    csv = [n for n in z.namelist() if n.lower().endswith(".csv")][0]
    df = pd.read_csv(io.BytesIO(z.read(csv)), sep=";", header=None, names=["ts", "open", "high", "low", "close", "vol"])
    t = pd.to_datetime(df["ts"], format="%Y%m%d %H%M%S") + pd.Timedelta(hours=5)  # EST->~UTC
    out = df[["open", "high", "low", "close"]].copy()
    out.insert(0, "time", t)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", required=True)
    ap.add_argument("--start", type=int, required=True)
    ap.add_argument("--end", type=int, required=True)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    frames = []
    for y in range(args.start, args.end + 1):
        try:
            frames.append(parse_year(fetch_year(args.pair, y)))
            print(f"  {args.pair} {y}: {len(frames[-1]):,} bars")
        except Exception as e:
            print(f"  {args.pair} {y}: ERR {type(e).__name__} {str(e)[:60]}")
    if not frames:
        return
    full = pd.concat(frames).drop_duplicates("time").sort_values("time").reset_index(drop=True)
    dest = OUT / f"{args.pair.upper()}_M1.csv"
    full.to_csv(dest, index=False)
    print(f"-> {dest}  ({len(full):,} bars, {full['time'].min()} .. {full['time'].max()})")


if __name__ == "__main__":
    main()
