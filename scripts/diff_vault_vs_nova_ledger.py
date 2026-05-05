"""Compare vault Python reference trade ledger against nova engine ledger.

Goal: localize where the two implementations of IRB v5 disagree on the same
data, so we can target the bug in `novatrade.backtest.engine` without
reading 1,500 lines cold.

Output:
  - matched-by-entry coverage (which engine took which trades)
  - PnL distribution per partition (vault_only / nova_only / both)
  - exit-reason mix per partition
  - 20 largest "both took it but PnL differs" cases
  - first 20 vault_only and first 20 nova_only entries (with bar context)
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

REPO = Path("/home/nova/nova-core")
NOVA_CSV = REPO / "OUTPUT/backtest_10yr_irb_v5_20260429T090437Z_trades.csv"
VAULT_CSV = REPO / "OUTPUT/backtest_vault_irb_v5_matched_20260429T110208Z_trades.csv"
OUT_DIR = REPO / "OUTPUT"


def load_nova(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["entry_ts"] = pd.to_datetime(df["entry_ts"])
    df["exit_ts"] = pd.to_datetime(df["exit_ts"])
    df["side"] = df["side"].str.upper()
    df["source"] = "nova"
    return df


def load_vault_round_trips(path: Path) -> pd.DataFrame:
    """Vault ledger has one row per leg (entry, partial_tp, runner_stop, etc.).
    Group into round-trip records with one row per entry.
    """
    df = pd.read_csv(path)
    df["time"] = pd.to_datetime(df["time"])
    df["side"] = df["side"].str.upper()

    # Each entry begins a round trip; subsequent exit rows belong to it
    # until the next entry row.
    rounds: list[dict] = []
    cur: dict | None = None
    for _, row in df.iterrows():
        if row["type"] == "entry":
            if cur is not None:
                rounds.append(cur)
            cur = {
                "entry_ts": row["time"],
                "side": row["side"],
                "entry_price": float(row["price"]),
                "qty": float(row["qty"]),
                "exit_ts": None,
                "exit_price": None,
                "pnl_total": 0.0,
                "n_exit_legs": 0,
                "exit_reasons": [],
            }
        elif row["type"] == "exit" and cur is not None:
            cur["exit_ts"] = row["time"]
            cur["exit_price"] = float(row["price"])
            cur["pnl_total"] += float(row["pnl"]) if pd.notna(row["pnl"]) else 0.0
            cur["n_exit_legs"] += 1
            cur["exit_reasons"].append(str(row["reason"]) if pd.notna(row["reason"]) else "")
    if cur is not None:
        rounds.append(cur)

    out = pd.DataFrame(rounds)
    out["exit_reason"] = out["exit_reasons"].apply(lambda r: "+".join(r) if r else "")
    out = out.drop(columns=["exit_reasons"])
    out["source"] = "vault"
    return out


def normalize_for_merge(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """Return uniform columns for join."""
    if source == "nova":
        cols = {
            "entry_ts": df["entry_ts"],
            "side": df["side"],
            "entry_price": df["entry_price"],
            "exit_ts": df["exit_ts"],
            "exit_price": df["exit_price"],
            "pnl_usd": df["pnl_usd"],
            "exit_reason": df["exit_reason"].astype(str),
            "volume": df["volume"],
        }
    else:
        cols = {
            "entry_ts": df["entry_ts"],
            "side": df["side"],
            "entry_price": df["entry_price"],
            "exit_ts": df["exit_ts"],
            "exit_price": df["exit_price"],
            "pnl_usd": df["pnl_total"],
            "exit_reason": df["exit_reason"].astype(str),
            "volume": df["qty"],
        }
    norm = pd.DataFrame(cols)
    norm["source"] = source
    return norm


def main() -> None:
    print(f"[load] nova:  {NOVA_CSV.name}")
    nova_raw = load_nova(NOVA_CSV)
    nova = normalize_for_merge(nova_raw, "nova")
    print(f"        {len(nova):,} round-trip trades")

    print(f"[load] vault: {VAULT_CSV.name}")
    vault_raw = load_vault_round_trips(VAULT_CSV)
    vault = normalize_for_merge(vault_raw, "vault")
    print(f"        {len(vault):,} round-trip trades")

    # ----------------------------------------------------------------------
    # Bucket-time alignment
    # ----------------------------------------------------------------------
    # The two engines may disagree on entry_ts by a bar (5-min bucket) due
    # to different intra-bar fill ordering. Round both to 5-min bucket so
    # alignment is bar-level, not microsecond.
    nova["bucket"] = nova["entry_ts"].dt.floor("5min")
    vault["bucket"] = vault["entry_ts"].dt.floor("5min")

    # Merge on (bucket, side). Outer join so we see both-only and only-one cases.
    merged = pd.merge(
        nova.rename(columns={c: f"nova_{c}" for c in nova.columns if c not in ("bucket", "side")}),
        vault.rename(columns={c: f"vault_{c}" for c in vault.columns if c not in ("bucket", "side")}),
        on=["bucket", "side"],
        how="outer",
        indicator=True,
    )

    matched = merged[merged["_merge"] == "both"]
    nova_only = merged[merged["_merge"] == "left_only"]
    vault_only = merged[merged["_merge"] == "right_only"]

    print()
    print("## Coverage")
    print(f"  matched (both engines took, same bucket+side): {len(matched):,}")
    print(f"  nova_only:  {len(nova_only):,}")
    print(f"  vault_only: {len(vault_only):,}")
    print()
    print(f"  nova total:  {len(nova):,}  (matched + nova_only = {len(matched) + len(nova_only):,})")
    print(f"  vault total: {len(vault):,}  (matched + vault_only = {len(matched) + len(vault_only):,})")

    # ----------------------------------------------------------------------
    # PnL by partition
    # ----------------------------------------------------------------------
    print()
    print("## PnL totals by partition")
    print(f"  nova on matched trades:  ${matched['nova_pnl_usd'].sum():>12,.2f}")
    print(f"  vault on matched trades: ${matched['vault_pnl_usd'].sum():>12,.2f}")
    print(f"  nova on nova_only:       ${nova_only['nova_pnl_usd'].sum():>12,.2f}")
    print(f"  vault on vault_only:     ${vault_only['vault_pnl_usd'].sum():>12,.2f}")

    # On matched trades: how often does the sign agree?
    sign_agree = ((matched["nova_pnl_usd"] > 0) == (matched["vault_pnl_usd"] > 0)).sum()
    sign_disagree = len(matched) - sign_agree
    print()
    print(f"## Matched trades — sign agreement: {sign_agree:,} ({sign_agree / max(1, len(matched)) * 100:.1f}%)")
    print(f"   sign disagreement: {sign_disagree:,} ({sign_disagree / max(1, len(matched)) * 100:.1f}%)")
    print()
    pnl_diff = matched["nova_pnl_usd"] - matched["vault_pnl_usd"]
    print("## Matched trades — nova_pnl − vault_pnl distribution")
    print(f"   mean:    ${pnl_diff.mean():>10,.2f}")
    print(f"   median:  ${pnl_diff.median():>10,.2f}")
    print(f"   p10:     ${pnl_diff.quantile(0.10):>10,.2f}")
    print(f"   p90:     ${pnl_diff.quantile(0.90):>10,.2f}")
    print(f"   stdev:   ${pnl_diff.std():>10,.2f}")

    # ----------------------------------------------------------------------
    # Exit reason mix
    # ----------------------------------------------------------------------
    def reason_mix(s: pd.Series) -> dict:
        c: Counter[str] = Counter()
        for r in s.fillna(""):
            c[str(r)] += 1
        total = sum(c.values()) or 1
        return {k: f"{v} ({v / total * 100:.1f}%)" for k, v in c.most_common()}

    print()
    print("## Exit-reason mix — matched trades")
    print(f"   nova exits:  {reason_mix(matched['nova_exit_reason'])}")
    print(f"   vault exits: {reason_mix(matched['vault_exit_reason'])}")

    print()
    print("## Exit-reason mix — nova_only")
    print(f"   {reason_mix(nova_only['nova_exit_reason'])}")
    print()
    print("## Exit-reason mix — vault_only")
    print(f"   {reason_mix(vault_only['vault_exit_reason'])}")

    # ----------------------------------------------------------------------
    # Largest "both took it, PnL differs" cases
    # ----------------------------------------------------------------------
    matched = matched.copy()
    matched["pnl_diff"] = matched["nova_pnl_usd"] - matched["vault_pnl_usd"]
    matched["abs_pnl_diff"] = matched["pnl_diff"].abs()
    biggest = matched.nlargest(10, "abs_pnl_diff")
    print()
    print("## Top 10 matched trades by |nova_pnl − vault_pnl|")
    print()
    cols = [
        "bucket",
        "side",
        "nova_entry_price",
        "vault_entry_price",
        "nova_exit_price",
        "vault_exit_price",
        "nova_exit_reason",
        "vault_exit_reason",
        "nova_pnl_usd",
        "vault_pnl_usd",
        "pnl_diff",
    ]
    print(biggest[cols].to_string(index=False))

    # ----------------------------------------------------------------------
    # First 10 vault_only and nova_only divergences (chronological)
    # ----------------------------------------------------------------------
    print()
    print("## First 10 vault_only entries (vault took, nova skipped)")
    cols_v = ["bucket", "side", "vault_entry_price", "vault_exit_price", "vault_exit_reason", "vault_pnl_usd"]
    vo = vault_only.sort_values("bucket").head(10)
    print(vo[cols_v].to_string(index=False))

    print()
    print("## First 10 nova_only entries (nova took, vault skipped)")
    cols_n = ["bucket", "side", "nova_entry_price", "nova_exit_price", "nova_exit_reason", "nova_pnl_usd"]
    no = nova_only.sort_values("bucket").head(10)
    print(no[cols_n].to_string(index=False))

    # ----------------------------------------------------------------------
    # Year-by-year coverage
    # ----------------------------------------------------------------------
    matched["year"] = matched["bucket"].dt.year
    nova_only_y = nova_only.copy()
    nova_only_y["year"] = nova_only_y["bucket"].dt.year
    vault_only_y = vault_only.copy()
    vault_only_y["year"] = vault_only_y["bucket"].dt.year

    print()
    print("## Year-by-year breakdown")
    print()
    print(f"{'Year':>6} | {'Matched':>8} | {'NovaOnly':>9} | {'VaultOnly':>10} | {'Nova$/yr':>12} | {'Vault$/yr':>12}")
    years = sorted(set(matched["year"]) | set(nova_only_y["year"]) | set(vault_only_y["year"]))
    for y in years:
        m = (matched["year"] == y).sum()
        n = (nova_only_y["year"] == y).sum()
        v = (vault_only_y["year"] == y).sum()
        nova_total = (nova_raw["entry_ts"].dt.year == y).pipe(lambda x: nova_raw[x]["pnl_usd"].sum())
        vault_total = (vault_raw["entry_ts"].dt.year == y).pipe(lambda x: vault_raw[x]["pnl_total"].sum())
        print(f"{y:>6} | {m:>8} | {n:>9} | {v:>10} | ${nova_total:>10,.2f} | ${vault_total:>10,.2f}")

    # ----------------------------------------------------------------------
    # Save merged ledger for downstream forensics
    # ----------------------------------------------------------------------
    ts_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_csv = OUT_DIR / f"diff_vault_vs_nova_{ts_tag}.csv"
    merged.to_csv(out_csv, index=False)
    print()
    print(f"[saved merged ledger] {out_csv}")


if __name__ == "__main__":
    main()
