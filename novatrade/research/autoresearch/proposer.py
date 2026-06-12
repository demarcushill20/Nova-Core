"""LLM-proposer layer — the "Karpathy" hypothesis generator for the loop.

An LLM reads the current leaderboard (what worked, what's been tried, what
failed) and proposes the next batch of candidates to score. The gauntlet then
judges them unchanged. The design keeps the JUDGE INCORRUPTIBLE:

  - the LLM may only propose into EXISTING families (it cannot inject a backtest
    function), and may emit free-text `new_family_request`s for genuinely novel
    ideas that a human/coding-agent implements later;
  - `grounded` is FORCED from family membership, never taken from the LLM — so a
    proposer cannot label a breakout "structural" to slip past the mechanism
    gate. The LLM's `mechanism`/`hypothesis` text is kept for reporting only.

Transport is injectable: `LLMProposer(complete_fn)` takes any
`Callable[[str], str]` (Anthropic SDK, the `claude` CLI, a sub-agent, or a test
stub). `anthropic_complete_fn()` builds the SDK adapter when a key is present.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from novatrade.research.autoresearch.families import GROUNDED_FAMILIES, Candidate
from novatrade.research.autoresearch.gauntlet import Verdict

# family -> required param keys (the DSL the LLM must target)
FAMILY_PARAM_KEYS: dict[str, tuple[str, ...]] = {
    "hour_drift": ("hour", "direction", "hold", "stop_pips"),
    "session_drift": ("start_hour", "end_hour", "direction", "stop_pips"),
    "fix_reversal": ("fix_hour", "fix_minute", "pre_bars", "hold_bars", "stop_pips"),
    "mr_fade": ("bb_n", "bb_k", "atr_stop", "max_hold"),
    "breakout": ("session_open", "or_bars", "rr", "min_w", "max_w", "sess_hours"),
}

FAMILY_SPEC = """\
Families you may propose into (you CANNOT invent new ones here — for a novel
mechanism, add a "new_family_request" string instead):
  hour_drift   {hour:0-23, direction:-1|+1, hold:1-4, stop_pips:10-50}
               short/long a single UTC hour block. Structural (flow seasonality).
  session_drift{start_hour, end_hour (UTC, end>start), direction:-1|+1, stop_pips:20-80}
               hold across a session window. Structural (own-hours flow).
  fix_reversal {fix_hour (UTC), fix_minute:0, pre_bars:2-4, hold_bars:2-6, stop_pips}
               fade the pre-fix drift at a benchmark fix (16:00 WM/R London,
               ~13:00 ECB, ~01:00 Tokyo). Structural (fixing order-flow reversal).
  mr_fade      {bb_n, bb_k, atr_stop, max_hold}  Bollinger fade. NOT structural.
  breakout     {session_open, or_bars, rr, min_w, max_w, sess_hours}  ORB. NOT structural.
direction: -1 = SELL EURUSD (EUR weakness), +1 = BUY. All times UTC."""


@dataclass
class ProposalResult:
    candidates: list[Candidate]
    new_family_requests: list[str] = field(default_factory=list)
    raw: str = ""


def digest_leaderboard(leaderboard: list[Verdict], max_rows: int = 12) -> str:
    """Compact, prompt-ready summary of the current state."""
    lines = []
    deploys = [v for v in leaderboard if v.tier == "deploy"][:max_rows]
    watches = [v for v in leaderboard if v.tier == "watch"][:max_rows]
    rejects = [v for v in leaderboard if v.tier == "reject"]
    lines.append("DEPLOY (passed every gate incl. sealed hold-out + mechanism):")
    lines += [
        f"  {v.key}  trainSharpe={v.sharpe} OOS={v.holdout_sharpe} posYr={v.frac_pos_years}" for v in deploys
    ] or ["  (none yet)"]
    lines.append("WATCH (strong but ungrounded or unconfirmed):")
    lines += [f"  {v.key}  trainSharpe={v.sharpe} OOS={v.holdout_sharpe}" for v in watches] or ["  (none)"]
    fam_fail: dict[str, int] = {}
    for v in rejects:
        fam_fail[v.family] = fam_fail.get(v.family, 0) + 1
    lines.append(f"REJECTED counts by family: {fam_fail}")
    return "\n".join(lines)


def build_proposal_prompt(leaderboard: list[Verdict], n: int = 12) -> str:
    return f"""You are a quant researcher proposing the next batch of strategy candidates for
an automated EURUSD search. A sealed hold-out and a mechanism gate will judge
them — you cannot game those, so propose ideas with a real economic reason.

Current leaderboard:
{digest_leaderboard(leaderboard)}

{FAMILY_SPEC}

Propose {n} NEW candidates that are NOT already on the leaderboard. Favour
structural families (hour_drift / session_drift) explored in economically-motivated
places we haven't tried (other session boundaries, fixing windows, hold lengths).
For each give a one-line `mechanism` (WHY flow/structure would cause it).

Reply with ONLY a JSON object:
{{"proposals":[{{"family":"hour_drift","params":{{"hour":15,"direction":-1,"hold":1,"stop_pips":20}},"mechanism":"..."}}],
  "new_family_requests":["one-line spec of any novel family worth coding"]}}"""


def _coerce_params(family: str, params: dict) -> dict | None:
    keys = FAMILY_PARAM_KEYS.get(family)
    if keys is None:
        return None
    out: dict = {}
    for k in keys:
        if k not in params:
            return None
        try:
            out[k] = float(params[k])
        except (TypeError, ValueError):
            return None
    # light sanity clamps
    if family == "hour_drift" and not (0 <= out["hour"] <= 23 and out["stop_pips"] > 0):
        return None
    if family == "session_drift" and not (out["end_hour"] > out["start_hour"] and out["stop_pips"] > 0):
        return None
    return out


def parse_proposals(raw: str, tried_keys: set[str] | None = None) -> ProposalResult:
    """Extract candidates from an LLM reply. Robust to prose/fences. `grounded`
    is FORCED from family membership (the incorruptible-judge invariant)."""
    tried = tried_keys or set()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return ProposalResult([], [], raw)
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return ProposalResult([], [], raw)

    out: list[Candidate] = []
    seen = set(tried)
    for p in obj.get("proposals", []):
        fam = p.get("family")
        if fam not in FAMILY_PARAM_KEYS:
            continue
        params = _coerce_params(fam, p.get("params", {}))
        if params is None:
            continue
        if fam == "hour_drift":
            params = {
                **params,
                "hour": int(params["hour"]),
                "direction": int(params["direction"]),
                "hold": int(params.get("hold", 1)),
            }
        if fam == "session_drift":
            params = {
                **params,
                "start_hour": int(params["start_hour"]),
                "end_hour": int(params["end_hour"]),
                "direction": int(params["direction"]),
            }
        if fam == "fix_reversal":
            params = {
                **params,
                "fix_hour": int(params["fix_hour"]),
                "fix_minute": int(params["fix_minute"]),
                "pre_bars": int(params["pre_bars"]),
                "hold_bars": int(params["hold_bars"]),
            }
        cand = Candidate.make(
            fam,
            params,
            mechanism=str(p.get("mechanism", "llm-proposed"))[:120],
            grounded=(fam in GROUNDED_FAMILIES),  # FORCED — never from the LLM
        )
        if cand.key in seen:
            continue
        seen.add(cand.key)
        out.append(cand)
    reqs = [str(r)[:200] for r in obj.get("new_family_requests", []) if r]
    return ProposalResult(out, reqs, raw)


@dataclass
class LLMProposer:
    """Transport-agnostic LLM proposer. `complete_fn` is any prompt->text call."""

    complete_fn: Callable[[str], str]
    n: int = 12

    def __call__(self, leaderboard: list[Verdict], tried_keys: set[str] | None = None) -> ProposalResult:
        prompt = build_proposal_prompt(leaderboard, self.n)
        raw = self.complete_fn(prompt)
        return parse_proposals(raw, tried_keys)


def anthropic_complete_fn(model: str = "claude-opus-4-8", api_key: str | None = None, max_tokens: int = 2000):
    """Build a complete_fn backed by the Anthropic SDK. Raises a clear error if
    the SDK or key is missing — callers can fall back to the programmatic proposer."""
    import os

    try:
        import anthropic
    except ImportError as e:  # pragma: no cover - optional dep
        raise RuntimeError("anthropic SDK not installed (pip install anthropic)") from e
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    client = anthropic.Anthropic(api_key=key)

    def _complete(prompt: str) -> str:  # pragma: no cover - network
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")

    return _complete
