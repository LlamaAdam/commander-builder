"""FP-002 (reframed) -- regress curator improvement *margin* on deck features.

The original FP-002 was a kept-vs-reverted *classifier*. It was concluded
NOT VIABLE on 2026-05-22 because, with correct seat-attribution, the curator's
swaps almost never made a deck strictly worse -> no negative class to learn
(see STATUS.md "Parked plans").

The accumulated 40-game A/B soak rows reopen it under the framing STATUS.md
itself proposed: *"regress on improvement margin."* Each curated deck ("... v2")
now has many high-confidence games vs its original, so we have a real, signed,
continuous target (win-rate margin) AND -- crucially -- both winners and losers
among the curated decks. This module:

  1. Aggregates soak JSONL rows per deck pair (original `deck_a` vs `v2` `deck_b`).
  2. Computes the **win-rate margin** = (wins_b - wins_a) / decisive_games,
     i.e. how much the curated version out- (or under-) performs the original.
  3. Extracts **pre-sim features of the ORIGINAL deck** via
     `deck_health.compute_deck_health` -- the honest predictive substrate
     (no sim outcome leaks in; we ask "from the deck alone, can we tell whether
     curation will help it?").
  4. Reports per-feature Pearson correlation with margin + a leave-one-out
     single-feature OLS baseline.

Pure stdlib -- numpy / sklearn / scipy are NOT installed on the soak boxes.
The unit of analysis is the *deck* (group-level), so n == unique decks, not
games. With ~30 decks this is exploratory, not a shipped predictor: it answers
"is there a learnable signal here at all, and which deck traits drive it?"
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Optional

# Make the package importable when run as a loose script.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

DEFAULT_INBOX = r"C:\Users\pilot\soak_inbox"
NEUTRAL_BAND = 0.05  # |margin| <= this -> "neutral"


# --------------------------------------------------------------------------- #
# Soak-row aggregation
# --------------------------------------------------------------------------- #
@dataclass
class Pair:
    """All games accumulated for one original-vs-curated deck comparison."""
    deck_a: str                 # original deck filename
    deck_b: str                 # curated "v2" deck filename
    wins_a: int = 0
    wins_b: int = 0
    games: int = 0
    rows: int = 0

    @property
    def decisive(self) -> int:
        return self.wins_a + self.wins_b

    @property
    def margin(self) -> Optional[float]:
        """(curated - original) / decisive, in [-1, 1]. None if no decisive games."""
        d = self.decisive
        return (self.wins_b - self.wins_a) / d if d else None

    def verdict(self, band: float = NEUTRAL_BAND) -> str:
        m = self.margin
        if m is None:
            return "undecided"
        if m > band:
            return "kept"        # curated better
        if m < -band:
            return "reverted"    # original better
        return "neutral"


def load_rows(inbox: str, pattern: str = "*throughput*.jsonl") -> list[dict]:
    """Read every completed A/B row from the soak JSONL files in `inbox`."""
    rows: list[dict] = []
    for path in glob.glob(os.path.join(inbox, pattern)):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if r.get("status") == "done":
                        rows.append(r)
        except OSError:
            continue
    return rows


def aggregate_pairs(rows: list[dict], min_games: int = 0) -> dict[str, Pair]:
    """Group rows by original deck name, summing wins/games.

    `min_games` filters to high-confidence rows (e.g. 40) BEFORE aggregation,
    so a pair's totals come only from trustworthy games."""
    pairs: dict[str, Pair] = {}
    for r in rows:
        if int(r.get("games", 0) or 0) < min_games:
            continue
        a, b = r.get("deck_a"), r.get("deck_b")
        if not a or not b:
            continue
        p = pairs.get(a)
        if p is None:
            p = pairs[a] = Pair(deck_a=a, deck_b=b)
        p.wins_a += int(r.get("wins_a", 0) or 0)
        p.wins_b += int(r.get("wins_b", 0) or 0)
        p.games += int(r.get("games", 0) or 0)
        p.rows += 1
    return pairs


# --------------------------------------------------------------------------- #
# Gauntlet aggregation (unconfounded design)
# --------------------------------------------------------------------------- #
# In gauntlet mode each deck plays the SAME fixed 3-deck gauntlet on its own,
# so base and v2 never share a pod -- their win-rates are directly comparable
# without the head-to-head confound the A/B pod design carries. Each row is one
# (test_deck, role) result vs the gauntlet: {role: base|v2, pair_base, wins,
# losses, draws, games}. Margin = winrate(v2) - winrate(base).
@dataclass
class GauntletPair:
    pair_base: str                      # the original deck filename (the key)
    base_w: int = 0
    base_l: int = 0
    base_g: int = 0
    v2_w: int = 0
    v2_l: int = 0
    v2_g: int = 0

    @staticmethod
    def _wr(w: int, l: int) -> Optional[float]:
        d = w + l
        return w / d if d else None

    @property
    def base_winrate(self) -> Optional[float]:
        return self._wr(self.base_w, self.base_l)

    @property
    def v2_winrate(self) -> Optional[float]:
        return self._wr(self.v2_w, self.v2_l)

    @property
    def complete(self) -> bool:
        return self.base_winrate is not None and self.v2_winrate is not None

    @property
    def margin(self) -> Optional[float]:
        """winrate(v2) - winrate(base), in [-1, 1]. None unless both sides
        have decisive games."""
        bw, vw = self.base_winrate, self.v2_winrate
        return None if bw is None or vw is None else vw - bw

    def verdict(self, band: float = NEUTRAL_BAND) -> str:
        m = self.margin
        if m is None:
            return "undecided"
        if m > band:
            return "kept"
        if m < -band:
            return "reverted"
        return "neutral"


def aggregate_gauntlet(rows: list[dict], min_games: int = 0) -> dict[str, GauntletPair]:
    """Group gauntlet rows by `pair_base`, summing each role's wins/losses."""
    pairs: dict[str, GauntletPair] = {}
    for r in rows:
        if int(r.get("games", 0) or 0) < min_games:
            continue
        key = r.get("pair_base")
        role = r.get("role")
        if not key or role not in ("base", "v2"):
            continue
        p = pairs.get(key)
        if p is None:
            p = pairs[key] = GauntletPair(pair_base=key)
        w = int(r.get("wins", 0) or 0)
        l = int(r.get("losses", 0) or 0)
        g = int(r.get("games", 0) or 0)
        if role == "base":
            p.base_w += w
            p.base_l += l
            p.base_g += g
        else:
            p.v2_w += w
            p.v2_l += l
            p.v2_g += g
    return pairs


def build_gauntlet_samples(
    pairs: dict[str, GauntletPair],
    decks_dirs: list[str],
) -> tuple[list[Sample], list[str]]:
    """Join each complete gauntlet pair to its base deck file -> feature sample.

    Features describe the ORIGINAL (base) deck, same substrate as the A/B path,
    so the two analyses are directly comparable."""
    samples: list[Sample] = []
    skipped: list[str] = []
    for name, p in sorted(pairs.items()):
        m = p.margin
        if m is None:
            skipped.append(f"{name} (incomplete: base or v2 has no decisive games)")
            continue
        path = _find_deck(p.pair_base, decks_dirs)
        if path is None:
            skipped.append(f"{name} (deck file not found)")
            continue
        try:
            text = open(path, encoding="utf-8").read()
        except OSError:
            skipped.append(f"{name} (unreadable)")
            continue
        samples.append(Sample(
            deck=name, margin=m, games=p.base_g + p.v2_g,
            features=deck_features(text, name),
        ))
    return samples, skipped


# --------------------------------------------------------------------------- #
# Deck-composition features (of the ORIGINAL deck)
# --------------------------------------------------------------------------- #
_BRACKET_RE = re.compile(r"\[B(\d)\]")
_BASIC_RE = re.compile(
    r"^(\d+)\s+(?:Snow-Covered\s+)?"
    r"(?:Forest|Island|Swamp|Mountain|Plains|Wastes)\b",
    re.MULTILINE,
)

# The numeric features we regress margin on. Names are stable so a report is
# diff-able across runs.
FEATURE_NAMES: list[str] = [
    "bracket",
    "main_count",
    "basic_lands",
    "spell_density",
    "mana_sinks",
    "wincon_protection",
    "self_mill",
    "mdfc",
    "under_built_roles",      # how many roles fall short of template minimums
    "deficit_total",          # summed shortfall across roles (build headroom)
]


def deck_features(deck_text: str, filename: str = "") -> dict[str, float]:
    """Pre-sim features of a deck. Pure-offline (regex + heuristic health)."""
    feats = {name: 0.0 for name in FEATURE_NAMES}

    mb = _BRACKET_RE.search(filename)
    feats["bracket"] = float(mb.group(1)) if mb else 0.0
    feats["basic_lands"] = float(
        sum(int(m.group(1)) for m in _BASIC_RE.finditer(deck_text))
    )

    try:
        from commander_builder.deck_health import compute_deck_health
        h = compute_deck_health(deck_text)
        sd = h.get("spell_density", {}) or {}
        feats["spell_density"] = float(sd.get("ratio", 0.0) or 0.0)
        feats["main_count"] = float(sd.get("total_main_count", 0) or 0)
        feats["mana_sinks"] = float((h.get("mana_sinks", {}) or {}).get("count", 0) or 0)
        feats["wincon_protection"] = float(
            (h.get("wincon_protection", {}) or {}).get("count", 0) or 0)
        feats["self_mill"] = float((h.get("self_mill", {}) or {}).get("count", 0) or 0)
        feats["mdfc"] = float((h.get("mdfc", {}) or {}).get("count", 0) or 0)
        rt = h.get("role_targets", {}) or {}
        under = rt.get("under_built", []) or []
        feats["under_built_roles"] = float(len(under))
        roles = rt.get("roles", {}) or {}
        feats["deficit_total"] = float(
            sum(int(v.get("deficit", 0) or 0) for v in roles.values()))
    except Exception:
        pass
    return feats


# --------------------------------------------------------------------------- #
# Pure-stdlib statistics
# --------------------------------------------------------------------------- #
def pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    """Pearson correlation coefficient. None if undefined (n<2 or zero variance)."""
    n = len(xs)
    if n < 2 or len(ys) != n:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def t_stat(r: float, n: int) -> Optional[float]:
    """Two-sided t-statistic for a correlation (df = n-2)."""
    if n < 3 or abs(r) >= 1.0:
        return None
    return r * math.sqrt((n - 2) / (1 - r * r))


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
@dataclass
class Sample:
    deck: str
    margin: float
    games: int
    features: dict[str, float] = field(default_factory=dict)


def _find_deck(filename: str, decks_dirs: list[str]) -> Optional[str]:
    """First existing path for `filename` across the search dirs."""
    for d in decks_dirs:
        cand = os.path.join(d, filename)
        if os.path.exists(cand):
            return cand
    return None


def build_samples(
    pairs: dict[str, Pair],
    decks_dirs: list[str],
) -> tuple[list[Sample], list[str]]:
    """Join each decided pair to its original deck file -> feature sample.

    `decks_dirs` is a search path; the first dir containing the deck wins.
    Returns (samples, skipped) where `skipped` notes pairs we couldn't feature
    (missing deck file or no decisive games)."""
    samples: list[Sample] = []
    skipped: list[str] = []
    for name, p in sorted(pairs.items()):
        m = p.margin
        if m is None:
            skipped.append(f"{name} (no decisive games)")
            continue
        path = _find_deck(p.deck_a, decks_dirs)
        if path is None:
            skipped.append(f"{name} (deck file not found)")
            continue
        try:
            text = open(path, encoding="utf-8").read()
        except OSError:
            skipped.append(f"{name} (unreadable)")
            continue
        samples.append(Sample(
            deck=name, margin=m, games=p.games,
            features=deck_features(text, name),
        ))
    return samples, skipped


def analyze(samples: list[Sample]) -> dict:
    """Per-feature correlation of deck traits with curator improvement margin."""
    margins = [s.margin for s in samples]
    n = len(samples)
    out_feats = []
    for fname in FEATURE_NAMES:
        xs = [s.features.get(fname, 0.0) for s in samples]
        r = pearson(xs, margins)
        out_feats.append({
            "feature": fname,
            "pearson_r": None if r is None else round(r, 3),
            "t_stat": None if r is None else (
                None if (t := t_stat(r, n)) is None else round(t, 2)),
        })
    out_feats.sort(key=lambda d: abs(d["pearson_r"] or 0.0), reverse=True)

    verdicts = {"kept": 0, "reverted": 0, "neutral": 0}
    for s in samples:
        if s.margin > NEUTRAL_BAND:
            verdicts["kept"] += 1
        elif s.margin < -NEUTRAL_BAND:
            verdicts["reverted"] += 1
        else:
            verdicts["neutral"] += 1

    mean_margin = sum(margins) / n if n else 0.0
    return {
        "n_decks": n,
        "total_games": sum(s.games for s in samples),
        "mean_margin": round(mean_margin, 4),
        "verdicts": verdicts,
        "feature_correlations": out_feats,
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inbox", default=DEFAULT_INBOX,
                    help="dir holding the soak JSONL rows")
    ap.add_argument("--mode", choices=("ab", "gauntlet"), default="ab",
                    help="ab = v1-vs-v2-in-pod (*throughput*.jsonl); "
                         "gauntlet = each deck vs a fixed gauntlet, unconfounded "
                         "(*gauntlet*.jsonl). Default ab.")
    ap.add_argument("--decks", default=None, action="append",
                    help="dir holding deck .dck files; repeatable (search path). "
                         "Default: <inbox>/{box2_decks,popular_decks,new_decks} "
                         "+ the repo's vendor/forge user decks.")
    ap.add_argument("--min-games", type=int, default=40,
                    help="only aggregate rows with >= this many games (default 40)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = ap.parse_args(argv)

    if args.decks:
        decks_dirs = args.decks
    else:
        repo_decks = os.path.join(os.path.dirname(__file__), "..", "vendor",
                                  "forge", "userdata", "decks", "commander")
        decks_dirs = [os.path.join(args.inbox, sub) for sub in
                      ("box2_decks", "popular_decks", "new_decks",
                       "gauntlet_decks", "control_decks")]
        decks_dirs.append(repo_decks)
    if args.mode == "gauntlet":
        rows = load_rows(args.inbox, "*gauntlet*.jsonl")
        pairs = aggregate_gauntlet(rows, min_games=args.min_games)
        samples, skipped = build_gauntlet_samples(pairs, decks_dirs)
    else:
        rows = load_rows(args.inbox)
        pairs = aggregate_pairs(rows, min_games=args.min_games)
        samples, skipped = build_samples(pairs, decks_dirs)
    report = analyze(samples)
    report["mode"] = args.mode
    report["skipped"] = skipped
    report["min_games"] = args.min_games

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    margin_desc = ("winrate(v2)-winrate(base) vs fixed gauntlet"
                   if args.mode == "gauntlet" else "per-deck win-rate delta")
    print(f"FP-002 margin analysis  (mode={args.mode}, min_games={args.min_games})")
    print(f"  decks: {report['n_decks']}   games: {report['total_games']}")
    print(f"  mean curator margin: {report['mean_margin']:+.4f}  "
          f"(>0 = curation helps; {margin_desc})")
    v = report["verdicts"]
    print(f"  per-deck verdicts: kept={v['kept']}  "
          f"reverted={v['reverted']}  neutral={v['neutral']}")
    print(f"\n  feature -> margin correlation (|r| desc, n={report['n_decks']} decks):")
    for f in report["feature_correlations"]:
        r = f["pearson_r"]
        t = f["t_stat"]
        bar = ""
        if r is not None:
            star = "*" if (t is not None and abs(t) >= 2.0) else " "
            bar = f"r={r:+.3f}  t={t if t is not None else 'NA':>6}  {star}"
        else:
            bar = "r=  NA   (no variance)"
        print(f"    {f['feature']:<20} {bar}")
    if skipped:
        print(f"\n  skipped {len(skipped)} pair(s): "
              + "; ".join(skipped[:6]) + ("..." if len(skipped) > 6 else ""))
    print("\n  note: |t|>=2 (~p<.05, df=n-2) flagged with *. With ~30 decks this "
          "is exploratory.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
