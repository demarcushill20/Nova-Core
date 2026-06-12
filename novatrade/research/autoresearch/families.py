"""Candidate strategy families for the autoresearch loop.

A `Candidate` is a (family, params, mechanism, grounded) tuple the loop can
generate and the gauntlet can score. Each family backtest returns a per-trade
DataFrame[entry_time, gross_pips, stop_pips]; the gauntlet applies cost and
normalises to R = (gross_pips - cost) / stop_pips (sizing-independent).

`grounded` marks whether the family has a STRUCTURAL reason to work — flow /
session seasonality (True) vs a pure indicator pattern (False). The mechanism
gate uses it: ungrounded survivors are reported as "watch", not "deploy", no
matter how good the curve looks. This is the line that separated the real edges
(drift) from the cost-mirages (fade, breakout) in the manual search.

Stop handling for the drift families is CONSERVATIVE: on the holding window's
hourly bars, if the adverse extreme breached the stop we book a full -stop_pips
(worst-case the stop hit first); otherwise we exit at the window close. The final
survivor still gets a tick-level confirmation outside this loop.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from novatrade.research.autoresearch.data import Dataset

PIP = 1e-4


@dataclass(frozen=True)
class Candidate:
    family: str
    params: tuple[tuple[str, float], ...]  # sorted (key, value) pairs -> hashable
    mechanism: str
    grounded: bool

    @staticmethod
    def make(family: str, params: dict, mechanism: str, grounded: bool) -> Candidate:
        return Candidate(family, tuple(sorted(params.items())), mechanism, grounded)

    def pdict(self) -> dict:
        return dict(self.params)

    @property
    def key(self) -> str:
        return f"{self.family}:{dict(self.params)}"


# ----------------------------- drift families (hourly) -----------------------------
def _hour_block_pnl(
    hourly: pd.DataFrame, hours: list[int], direction: int, stop_pips: float, relative_pip: bool = False
) -> pd.DataFrame:
    """Hold `direction` across the given UTC hours each weekday; conservative
    stop at `stop_pips`. One trade/day per contiguous block (here: one block).
    `relative_pip`: measure moves/stop in bps of entry price (crypto), so a fixed
    stop_pips stays meaningful across a wide price range."""
    h = hourly[hourly.index.weekday < 5]
    sub = h[h.index.hour.isin(hours)].copy()
    if sub.empty:
        return pd.DataFrame(columns=["entry_time", "gross_pips", "stop_pips"])
    sub["date"] = sub.index.date
    rows = []
    for _date, g in sub.groupby("date"):
        g = g.sort_index()
        entry = g["open"].iloc[0]
        unit = entry * PIP if relative_pip else PIP  # price value of one "pip"
        if direction > 0:  # long: adverse = drop below entry
            adverse = (entry - g["low"].min()) / unit
        else:  # short: adverse = rise above entry
            adverse = (g["high"].max() - entry) / unit
        if adverse >= stop_pips:
            gross = -stop_pips
        else:
            gross = direction * (g["close"].iloc[-1] - entry) / unit
        rows.append((g.index[0], gross, stop_pips))
    return pd.DataFrame(rows, columns=["entry_time", "gross_pips", "stop_pips"])


def _bt_hour_drift(ds: Dataset, p: dict) -> pd.DataFrame:
    hour = int(p["hour"])
    hold = int(p.get("hold", 1))
    hours = list(range(hour, hour + hold))
    return _hour_block_pnl(ds.hourly, hours, int(p["direction"]), float(p["stop_pips"]), ds.relative_pip)


def _bt_session_drift(ds: Dataset, p: dict) -> pd.DataFrame:
    h0, h1 = int(p["start_hour"]), int(p["end_hour"])
    hours = list(range(h0, h1))
    return _hour_block_pnl(ds.hourly, hours, int(p["direction"]), float(p["stop_pips"]), ds.relative_pip)


# ----------------------------- control families (15m) -----------------------------
def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - df["close"].shift()).abs(), (df["low"] - df["close"].shift()).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def _bt_mr_fade(ds: Dataset, p: dict) -> pd.DataFrame:
    """Bollinger 2σ fade, target=basis, ATR stop, time-exit. Ungrounded control."""
    df = ds.bars15
    n = int(p["bb_n"])
    k = float(p["bb_k"])
    hold = int(p.get("max_hold", 12))
    atr_mult = float(p.get("atr_stop", 1.0))
    basis = df["close"].rolling(n).mean()
    sd = df["close"].rolling(n).std()
    upper, lower = basis + k * sd, basis - k * sd
    atr = _atr(df, 14)
    hours = df.index.hour
    in_sess = (hours >= 7) & (hours < 20)
    close = df["close"].to_numpy()
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    up = upper.to_numpy()
    lo = lower.to_numpy()
    bs = basis.to_numpy()
    at = atr.to_numpy()
    idx = df.index
    rows = []
    i = n + 1
    N = len(df)
    while i < N - 1:
        if not in_sess[i] or np.isnan(up[i]) or np.isnan(at[i]):
            i += 1
            continue
        short = high[i] >= up[i] and close[i] < up[i]
        long = low[i] <= lo[i] and close[i] > lo[i]
        if not (short or long):
            i += 1
            continue
        d = -1 if short else 1
        entry = close[i]
        tgt = bs[i]
        stop_dist = atr_mult * at[i]
        stop = entry - d * stop_dist
        stop_pips = stop_dist / PIP
        gross = None
        j = i
        for j in range(i + 1, min(i + 1 + hold, N)):
            if d == 1:
                if low[j] <= stop:
                    gross = (stop - entry) / PIP
                    break
                if high[j] >= tgt:
                    gross = (tgt - entry) / PIP
                    break
            else:
                if high[j] >= stop:
                    gross = (entry - stop) / PIP
                    break
                if low[j] <= tgt:
                    gross = (entry - tgt) / PIP
                    break
        if gross is None:
            j = min(i + hold, N - 1)
            gross = d * (close[j] - entry) / PIP
        rows.append((idx[i], gross, max(stop_pips, 0.1)))
        i = j + 1
    return pd.DataFrame(rows, columns=["entry_time", "gross_pips", "stop_pips"])


def _bt_breakout(ds: Dataset, p: dict) -> pd.DataFrame:
    """Opening-range breakout, width-gated, close-confirmed. Ungrounded control."""
    df = ds.bars15
    so = int(p["session_open"])
    or_bars = int(p.get("or_bars", 2))
    rr = float(p.get("rr", 2.0))
    minw, maxw = float(p.get("min_w", 8)), float(p.get("max_w", 40))
    sess_end = so + int(p.get("sess_hours", 10))
    h = df.index.hour
    df = df.copy()
    df["date"] = df.index.date
    rows = []
    for _date, g in df[(h >= so) & (h < sess_end)].groupby("date"):
        g = g.sort_index()
        if len(g) < or_bars + 2:
            continue
        orb = g.iloc[:or_bars]
        hi, lo = orb["high"].max(), orb["low"].min()
        width = (hi - lo) / PIP
        if width < minw or width > maxw:
            continue
        post = g.iloc[or_bars:]
        pos = 0
        entry = stop = tgt = 0.0
        et = None
        for _, bar in post.iterrows():
            if pos == 0:
                if bar["close"] > hi:
                    pos, entry, et = 1, bar["close"], bar.name
                    risk = entry - lo
                    stop, tgt = lo, entry + rr * risk
                elif bar["close"] < lo:
                    pos, entry, et = -1, bar["close"], bar.name
                    risk = hi - entry
                    stop, tgt = hi, entry - rr * risk
            else:
                sp = abs(entry - stop) / PIP
                if pos == 1 and bar["low"] <= stop:
                    rows.append((et, (stop - entry) / PIP, sp))
                    pos = 0
                    break
                if pos == 1 and bar["high"] >= tgt:
                    rows.append((et, (tgt - entry) / PIP, sp))
                    pos = 0
                    break
                if pos == -1 and bar["high"] >= stop:
                    rows.append((et, (entry - stop) / PIP, sp))
                    pos = 0
                    break
                if pos == -1 and bar["low"] <= tgt:
                    rows.append((et, (entry - tgt) / PIP, sp))
                    pos = 0
                    break
        if pos != 0:
            last = post.iloc[-1]["close"]
            rows.append((et, pos * (last - entry) / PIP, abs(entry - stop) / PIP))
    return pd.DataFrame(rows, columns=["entry_time", "gross_pips", "stop_pips"])


def _bt_fix_reversal(ds: Dataset, p: dict) -> pd.DataFrame:
    """Benchmark-fix reversal (Krohn & Sushko 2024; Evans 2017). Dealers absorb
    fixing order flow into a benchmark fix (WM/R 16:00 London, ECB ~13:00, Tokyo
    ~01:00 UTC) and unwind after, so the pre-fix drift tends to REVERSE post-fix.
    Enter at the fix bar's open OPPOSITE the pre-fix drift; hold `hold_bars`;
    conservative stop. Structural order-flow mechanism (grounded) — but the
    literature warns it is marginal-to-negative net of retail cost, so the cost +
    hold-out + stability gates decide. relative_pip-aware."""
    df = ds.bars15
    fix_h, fix_m = int(p["fix_hour"]), int(p.get("fix_minute", 0))
    pre, hold = int(p.get("pre_bars", 2)), int(p.get("hold_bars", 4))
    stop_pips, relative = float(p["stop_pips"]), ds.relative_pip
    idx = df.index
    o, hi, lo = df["open"].to_numpy(), df["high"].to_numpy(), df["low"].to_numpy()
    hours, mins = idx.hour.to_numpy(), idx.minute.to_numpy()
    ns = idx.asi8
    bar_ns = 15 * 60 * 1_000_000_000
    fix_pos = np.where((hours == fix_h) & (mins == fix_m))[0]
    n = len(df)
    rows = []
    for i in fix_pos:
        if i - pre < 0 or i + hold >= n:
            continue
        # windows must be contiguous (no weekend/missing-bar gap across the fix)
        if ns[i] - ns[i - pre] > (pre + 1) * bar_ns or ns[i + hold] - ns[i] > (hold + 1) * bar_ns:
            continue
        entry = o[i]
        pre_drift = o[i] - o[i - pre]
        if pre_drift == 0:
            continue
        direction = -1 if pre_drift > 0 else 1  # fade the pre-fix move
        unit = entry * PIP if relative else PIP
        stop = entry - direction * stop_pips * unit
        gross = None
        for j in range(i, i + hold):  # holding bars
            if direction == 1 and lo[j] <= stop:
                gross = -stop_pips
                break
            if direction == -1 and hi[j] >= stop:
                gross = -stop_pips
                break
        if gross is None:
            gross = direction * (o[i + hold] - entry) / unit
        rows.append((idx[i], gross, stop_pips))
    return pd.DataFrame(rows, columns=["entry_time", "gross_pips", "stop_pips"])


_FAMILIES = {
    "hour_drift": _bt_hour_drift,
    "session_drift": _bt_session_drift,
    "fix_reversal": _bt_fix_reversal,
    "mr_fade": _bt_mr_fade,
    "breakout": _bt_breakout,
}

# Families with a structural (flow/order-imbalance) mechanism — pass the gate.
GROUNDED_FAMILIES = {"hour_drift", "session_drift", "fix_reversal"}


def backtest(c: Candidate, ds: Dataset) -> pd.DataFrame:
    """Dispatch a candidate to its family backtest -> per-trade DataFrame."""
    trades = _FAMILIES[c.family](ds, c.pdict())
    if len(trades):
        trades["entry_time"] = pd.to_datetime(trades["entry_time"])
    return trades
