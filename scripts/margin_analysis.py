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

FP-002 closed REFUTED on 2026-07-30: at n=93 no deck_health feature predicts
curator margin. The closure names the only honest reopening path -- NEW
regressors, not more games on the same features. `--features` selects the
candidate substrate:

  * deck_health (default) -- the original 10 features; byte-identical output.
  * clusters   -- corpus-theme cluster labels (corpus_themes' transparent
    ladder, applied to the base deck), tested one-vs-rest as 0/1 indicators
    (point-biserial == Pearson on the indicator).
  * card_score -- FP-015's deck-level CardScore components
    (bubble_analysis.score_deck: total, role_fit, mana_fit, salt_fit,
    reference_alignment) as continuous regressors. Called through the
    internal entry directly -- no env flag is flipped.
  * tournament -- FP-017 cEDH tournament card statistics (edhtop16) projected
    onto the base deck. **Bracket-5 decks only** and **cache-only** (no
    network inside a regression loop): a non-B5 deck reports every feature as
    None, exactly like any other unavailable component. Exploratory: the
    source describes bracket-5 humans and has never passed a gate.
  * all        -- every family, each reported with its own multiple-testing
    honesty line (features tested + expected false positives at p<.05),
    because FP-002's history shows exactly how a lone |t|>=2 hit misleads.

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

# Candidate feature substrates (FP-002 closure: reopening requires NEW
# regressors). "deck_health" is the original family and the default.
FEATURE_FAMILIES = ("deck_health", "clusters", "card_score", "tournament",
                    "all")

# A corpus-theme cluster only gets its own one-vs-rest indicator with at
# least this many member decks; thinner clusters lump into "other" (the
# same small-N refusal corpus_themes.MIN_CLUSTER_FOR_NORMS encodes).
CLUSTER_MIN_N = 5


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
                    # 'loop_unattributed' rows are honest SHORT rows: the
                    # batch was cut by a looping game no seat could be
                    # credited for, but the games they carry all completed.
                    # They participate here and are gated by --min-games like
                    # any other row (a 17-game row is legitimately sub-40).
                    if r.get("status") in ("done", "loop_unattributed"):
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
    rows: int = 0                       # soak rows folded into this pair

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
        p.rows += 1
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
    features: str = "deck_health",
) -> tuple[list[Sample], list[str], dict[str, int]]:
    """Join each complete gauntlet pair to its base deck file -> feature sample.

    Features describe the ORIGINAL (base) deck, same substrate as the A/B path,
    so the two analyses are directly comparable.

    Returns ``(samples, skipped, missing_decks)`` where ``missing_decks``
    maps each deck filename that could NOT be found on disk to the number of
    soak rows dropped because of it. A loud per-file warning is printed to
    stderr (once per missing deck) so the exclusion is never silent."""
    samples: list[Sample] = []
    skipped: list[str] = []
    missing: dict[str, int] = {}
    for name, p in sorted(pairs.items()):
        m = p.margin
        if m is None:
            skipped.append(f"{name} (incomplete: base or v2 has no decisive games)")
            continue
        path = _find_deck(p.pair_base, decks_dirs)
        if path is None:
            skipped.append(f"{name} (deck file not found)")
            missing[p.pair_base] = p.rows
            _warn_missing_deck(p.pair_base, p.rows)
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            skipped.append(f"{name} (unreadable)")
            continue
        feats, cluster = _featurize(text, name, features)
        samples.append(Sample(
            deck=name, margin=m, games=p.base_g + p.v2_g,
            features=feats, cluster=cluster,
        ))
    return samples, skipped, missing


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
# Candidate substrate 1: corpus-theme cluster labels (FP-002 reopening path)
# --------------------------------------------------------------------------- #
# Label used when the classifier can't run (module import failure, empty
# deck, no oracle snapshots resolving at all still yields a real label --
# corpus_themes degrades to goodstuff-midrange, not an error).
CLUSTER_UNCLASSIFIED = "unclassified"


def assign_cluster(deck_text: str, lookup=None) -> str:
    """Corpus-theme cluster label of a deck, via corpus_themes' OWN ladder.

    Reproduces ``corpus_themes.profile_deck`` exactly (same ``_deck_entries``
    -> ``_facts`` -> ``_classify`` pipeline, real quantities preserved) but
    from deck TEXT instead of a path, so soak-joined decks classify by
    precisely the rules that cluster the corpus. Offline by construction:
    the default lookup is corpus_themes' cache-only snapshot read.
    """
    try:
        from commander_builder import corpus_themes, dck_utils
        from commander_builder.collection import name_key
        if lookup is None:
            lookup = corpus_themes.default_lookup
        entries = list(corpus_themes._deck_entries(deck_text))
        if not entries:
            return CLUSTER_UNCLASSIFIED
        commanders = dck_utils.section_card_names(deck_text, "Commander")
        facts = corpus_themes._facts(
            entries, lookup,
            commander_keys={name_key(c) for c in commanders})
        import statistics
        cmcs = facts["cmcs"]
        role_counts = dict(facts["role_counts"])
        if "win_condition" in role_counts:
            role_counts["finisher"] = (
                role_counts.get("finisher", 0)
                + role_counts.pop("win_condition"))
        profile = corpus_themes.DeckProfile(
            filename="<soak>", role="user", commanders=commanders,
            role_counts=role_counts,
            curve=facts["curve"],
            cmc_mean=(round(statistics.fmean(cmcs), 2) if cmcs else 0.0),
            cmc_median=(round(statistics.median(cmcs), 2) if cmcs else 0.0),
            land_count=facts["land_count"],
            color_count=len(facts["colors"]),
            tribes=facts["tribes"], motifs=facts["motifs"],
            artifact_count=facts["artifact_count"],
            enchantment_count=facts["enchantment_count"],
            card_keys=facts["card_keys"],
        )
        label, _reason = corpus_themes._classify(profile)
        return label
    except Exception:
        return CLUSTER_UNCLASSIFIED


# --------------------------------------------------------------------------- #
# Candidate substrate 2: FP-015 CardScore deck-level components
# --------------------------------------------------------------------------- #
# The deck-level components bubble_analysis.score_deck exposes. Values may
# legitimately be None ("unavailable != bad" is that module's contract:
# reference_alignment needs a fetched corpus we deliberately do not fetch
# here; salt_fit needs bracket<=3 + a cached salt map). None values are
# excluded per-feature with an honest per-feature n.
CARD_SCORE_FEATURES: list[str] = [
    "cs_total",
    "cs_role_fit",
    "cs_mana_fit",
    "cs_salt_fit",
    "cs_reference_alignment",
]


def card_score_features(deck_text: str,
                        filename: str = "") -> dict[str, Optional[float]]:
    """FP-015 deck-level CardScore components for one deck.

    Calls ``bubble_analysis.score_deck`` DIRECTLY (the flag-independent
    internal entry) -- ``COMMANDER_BUILDER_CARD_SCORE`` is never read or
    flipped. ``corpus=None`` on purpose: no network corpus fetch inside a
    regression loop; the corpus-dependent components simply report None.
    Bracket comes from the ``[Bn]`` filename tag (same parse as the
    deck_health family) so salt_fit gets its bracket gate input.
    """
    feats: dict[str, Optional[float]] = {n: None for n in CARD_SCORE_FEATURES}
    try:
        from commander_builder.bubble_analysis import score_deck
        mb = _BRACKET_RE.search(filename)
        bracket = int(mb.group(1)) if mb else None
        rep = score_deck(deck_text=deck_text, corpus=None, bracket=bracket)
        feats["cs_total"] = float(rep.total)
        comps = rep.components or {}
        for comp in ("role_fit", "mana_fit", "salt_fit",
                     "reference_alignment"):
            v = (comps.get(comp) or {}).get("value")
            feats["cs_" + comp] = float(v) if v is not None else None
    except Exception:
        pass
    return feats


# --------------------------------------------------------------------------- #
# Candidate substrate 3: FP-017 cEDH tournament card statistics
# --------------------------------------------------------------------------- #
# WHY this substrate exists: FP-002 (deck_health) and FP-015 (CardScore) both
# failed their gates, and one diagnosis is that every feature we had was
# EDHREC-derived *deckbuilding preference* with no human *win* data behind it.
# edhtop16 aggregates cEDH tournament results -- real humans actually winning
# games -- so it is a genuinely NEW substrate rather than more of the same.
#
# Two hard limits, both enforced in code below rather than in prose:
#   1. BRACKET 5 ONLY. cEDH card statistics must not silently inform B2-B4
#      analysis; a non-B5 deck reports every feature as None.
#   2. CACHE-ONLY. No network fetch inside a regression loop (same rule as
#      card_score_features' `corpus=None`). Nothing cached -> None, which the
#      report renders as "unavailable", never as zero.
# It remains EXPLORATORY. Nothing here is a predictor until it passes a gate.
TOURNAMENT_FEATURES: list[str] = [
    "tt_coverage",            # share of the deck the stats even cover
    "tt_mean_presence",       # mean tournament play rate of covered cards
    "tt_staple_share",        # share of deck cards at >= 50% presence
    "tt_fringe_share",        # share of deck cards at <= 10% presence
    "tt_mean_card_winrate",   # mean entry win rate of the covered cards
]

#: A card at or above this tournament presence is a cEDH staple.
TOURNAMENT_STAPLE_FLOOR = 0.5
#: ...and at or below this, fringe among winning lists.
TOURNAMENT_FRINGE_CEILING = 0.1


def tournament_features(deck_text: str, filename: str = "",
                        lookup=None) -> dict[str, Optional[float]]:
    """FP-017 tournament card statistics projected onto one deck.

    ``lookup(commander)`` defaults to
    ``edhtop16_client.load_cached_card_stats`` -- the cache-only entry that
    NEVER touches the network. Injectable so tests run offline on synthetic
    rows.

    Returns all-None unless the deck is tagged ``[B5]``: the FP-017 scope
    gate, mirrored from ``bubble_analysis``' corpus-side gate so neither
    consumer can drift.
    """
    feats: dict[str, Optional[float]] = {n: None for n in TOURNAMENT_FEATURES}
    mb = _BRACKET_RE.search(filename)
    bracket = int(mb.group(1)) if mb else None
    try:
        from commander_builder.edhtop16_client import CEDH_BRACKET
    except Exception:
        return feats
    if bracket != CEDH_BRACKET:
        return feats  # scope gate: cEDH stats describe bracket 5 only
    try:
        from commander_builder import dck_utils
        if lookup is None:
            from commander_builder.edhtop16_client import (
                load_cached_card_stats as lookup)
        commanders = dck_utils.section_card_names(deck_text, "Commander")
        stats: dict = {}
        for cmdr in commanders:
            stats = lookup(cmdr) or {}
            if stats:
                break
        main = [n for n in dck_utils.section_card_names(deck_text, "Main")]
        if not main:
            return feats
        covered = []
        wrs = []
        for name in main:
            rec = stats.get(name.strip().lower())
            if rec is None:
                continue
            covered.append(float(rec.presence))
            wr = rec.mean_entry_win_rate
            if wr is not None:
                wrs.append(float(wr))
        feats["tt_coverage"] = len(covered) / len(main)
        if not covered:
            # Coverage 0 is a real, honest measurement; the rates over an
            # empty set are not.
            return feats
        feats["tt_mean_presence"] = sum(covered) / len(covered)
        feats["tt_staple_share"] = (
            sum(1 for p in covered if p >= TOURNAMENT_STAPLE_FLOOR)
            / len(main))
        feats["tt_fringe_share"] = (
            sum(1 for p in covered if p <= TOURNAMENT_FRINGE_CEILING)
            / len(main))
        if wrs:
            feats["tt_mean_card_winrate"] = sum(wrs) / len(wrs)
    except Exception:
        pass
    return feats


def _featurize(deck_text: str, filename: str,
               features: str) -> tuple[dict, Optional[str]]:
    """(numeric features, cluster label) for the selected substrate(s).

    ``features == "deck_health"`` runs exactly the original path (and only
    it), keeping the default output byte-identical."""
    feats: dict = {}
    cluster: Optional[str] = None
    if features in ("deck_health", "all"):
        feats.update(deck_features(deck_text, filename))
    if features in ("clusters", "all"):
        cluster = assign_cluster(deck_text)
    if features in ("card_score", "all"):
        feats.update(card_score_features(deck_text, filename))
    if features in ("tournament", "all"):
        feats.update(tournament_features(deck_text, filename))
    return feats, cluster


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


def _ols_fit(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Ordinary least-squares slope + intercept of ys ~ xs.

    Returns ``(slope, intercept)``. A feature with no variance (constant
    column) yields ``slope == 0.0`` and ``intercept == mean(ys)`` rather
    than a ZeroDivisionError, so a constant column is safe."""
    m = len(xs)
    if m == 0:
        return 0.0, 0.0
    mx = sum(xs) / m
    my = sum(ys) / m
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return 0.0, my
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = my - slope * mx
    return slope, intercept


def single_feature_ols(samples: list["Sample"], feature: str) -> dict:
    """Pure-stdlib single-feature OLS fit of margin on `feature`, plus a
    leave-one-out cross-validated RMSE -- the honest out-of-sample error.

    Returns a dict with:
      "n"        : int   -- number of samples
      "slope"    : float -- OLS slope of margin ~ feature (0.0 when the
                            feature has no variance, so a constant column
                            is safe rather than a ZeroDivisionError)
      "intercept": float
      "r2"       : float -- coefficient of determination in [0, 1] (0.0 when
                            the feature is constant / no fit)
      "loo_rmse" : float -- root mean squared error of leave-one-out
                            predictions (refit on n-1 points, predict the
                            held-out one)
    """
    xs = [s.features.get(feature, 0.0) for s in samples]
    ys = [s.margin for s in samples]
    n = len(samples)

    slope, intercept = _ols_fit(xs, ys)

    # r2: 1 - SS_res / SS_tot, clamped to [0, 1]. SS_tot == 0 (or a constant
    # feature giving slope 0) -> no fit -> r2 = 0.0.
    if n == 0:
        return {"n": 0, "slope": 0.0, "intercept": 0.0, "r2": 0.0, "loo_rmse": 0.0}
    my = sum(ys) / n
    ss_tot = sum((y - my) ** 2 for y in ys)
    if ss_tot == 0:
        r2 = 0.0
    else:
        ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
        r2 = 1.0 - ss_res / ss_tot
        r2 = max(0.0, min(1.0, r2))

    # Leave-one-out RMSE: refit on the other n-1 points, predict the held-out
    # one. Needs at least 3 points to leave one out and still fit a line.
    if n < 3:
        loo_rmse = 0.0
    else:
        sq_err = 0.0
        for i in range(n):
            xs_i = xs[:i] + xs[i + 1:]
            ys_i = ys[:i] + ys[i + 1:]
            s_i, b_i = _ols_fit(xs_i, ys_i)
            pred = s_i * xs[i] + b_i
            sq_err += (ys[i] - pred) ** 2
        loo_rmse = math.sqrt(sq_err / n)

    return {
        "n": n,
        "slope": slope,
        "intercept": intercept,
        "r2": r2,
        "loo_rmse": loo_rmse,
    }


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
@dataclass
class Sample:
    deck: str
    margin: float
    games: int
    features: dict[str, float] = field(default_factory=dict)
    cluster: Optional[str] = None   # corpus-theme label (--features clusters)


def _find_deck(filename: str, decks_dirs: list[str]) -> Optional[str]:
    """First existing path for `filename` across the search dirs."""
    for d in decks_dirs:
        cand = os.path.join(d, filename)
        if os.path.exists(cand):
            return cand
    return None


def _warn_missing_deck(filename: str, rows_dropped: int) -> None:
    """Loud, per-file stderr warning for a soak row whose deck left the disk.

    Rows referencing a vanished deck (renamed/pruned from the library, e.g.
    '[USER] Black Mage Blitz SPDET5-931 [B4].dck') are EXCLUDED from the
    regression -- that is correct (we cannot feature a deck we cannot read),
    but it must never be silent, or n quietly shrinks between runs."""
    print(f"WARNING: deck file not found: {filename!r} -- "
          f"excluding {rows_dropped} soak row(s) from the analysis",
          file=sys.stderr)


def build_samples(
    pairs: dict[str, Pair],
    decks_dirs: list[str],
    features: str = "deck_health",
) -> tuple[list[Sample], list[str], dict[str, int]]:
    """Join each decided pair to its original deck file -> feature sample.

    `decks_dirs` is a search path; the first dir containing the deck wins.
    Returns (samples, skipped, missing_decks) where `skipped` notes pairs we
    couldn't feature (missing deck file or no decisive games) and
    `missing_decks` maps each deck file absent from disk to the number of
    soak rows dropped because of it (also warned loudly on stderr, once per
    missing deck)."""
    samples: list[Sample] = []
    skipped: list[str] = []
    missing: dict[str, int] = {}
    for name, p in sorted(pairs.items()):
        m = p.margin
        if m is None:
            skipped.append(f"{name} (no decisive games)")
            continue
        path = _find_deck(p.deck_a, decks_dirs)
        if path is None:
            skipped.append(f"{name} (deck file not found)")
            missing[p.deck_a] = p.rows
            _warn_missing_deck(p.deck_a, p.rows)
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            skipped.append(f"{name} (unreadable)")
            continue
        feats, cluster = _featurize(text, name, features)
        samples.append(Sample(
            deck=name, margin=m, games=p.games,
            features=feats, cluster=cluster,
        ))
    return samples, skipped, missing


def analyze(samples: list[Sample],
            feature_names: Optional[list[str]] = None) -> dict:
    """Per-feature correlation of deck traits with curator improvement margin.

    ``feature_names`` defaults to the deck_health family (the original
    behavior, byte-identical); pass ``[]`` when another substrate is the
    only one under test so no all-NA deck_health table is emitted."""
    if feature_names is None:
        feature_names = FEATURE_NAMES
    margins = [s.margin for s in samples]
    n = len(samples)
    out_feats = []
    for fname in feature_names:
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


def _honesty(n_tested: int, n_hits: int) -> dict:
    """Multiple-testing honesty numbers for one feature family.

    FP-002's own history is the reason this is mandatory: at n=66 a lone
    wincon_protection hit (1 of 10 features at p<.05, expected false
    positives 0.5) looked like signal and was refuted at n=93. Every
    family's report therefore states how many tests were run and how many
    |t|>=2 hits pure chance would produce."""
    return {
        "features_tested": n_tested,
        "expected_false_positives_p05": round(0.05 * n_tested, 2),
        "hits_abs_t_ge_2": n_hits,
    }


def _honesty_line(family: str, h: dict) -> str:
    return (f"  multiple-testing honesty ({family}): "
            f"{h['features_tested']} feature(s) tested; expect "
            f"~{h['expected_false_positives_p05']:.2f} false positive(s) at "
            f"p<.05 by chance alone; observed |t|>=2 hits: "
            f"{h['hits_abs_t_ge_2']}")


def analyze_clusters(samples: list[Sample],
                     min_n: int = CLUSTER_MIN_N) -> dict:
    """One-vs-rest corpus-theme cluster indicators vs curator margin.

    Each cluster with >= ``min_n`` member decks becomes a 0/1 indicator;
    Pearson on a binary x IS the point-biserial correlation, so the
    existing r/t machinery applies unchanged. Thinner clusters lump into
    "other" (reported, and tested as its own indicator when big enough)."""
    n = len(samples)
    margins = [s.margin for s in samples]
    counts: dict[str, int] = {}
    for s in samples:
        label = s.cluster or CLUSTER_UNCLASSIFIED
        counts[label] = counts.get(label, 0) + 1
    keep = {c for c, k in counts.items() if k >= min_n}
    lumped = sorted(c for c in counts if c not in keep)

    def group_of(s: Sample) -> str:
        label = s.cluster or CLUSTER_UNCLASSIFIED
        return label if label in keep else "other"

    groups: dict[str, list[Sample]] = {}
    for s in samples:
        groups.setdefault(group_of(s), []).append(s)

    out: list[dict] = []
    n_tested = 0
    n_hits = 0
    for label in sorted(groups, key=lambda g: (-len(groups[g]), g)):
        members = groups[label]
        k = len(members)
        mean_m = sum(m.margin for m in members) / k
        r = t = None
        if k >= min_n:
            xs = [1.0 if group_of(s) == label else 0.0 for s in samples]
            r = pearson(xs, margins)      # None when indicator has no variance
            if r is not None:
                n_tested += 1
                t = t_stat(r, n)
                if t is not None and abs(t) >= 2.0:
                    n_hits += 1
        out.append({
            "cluster": label,
            "n": k,
            "mean_margin": round(mean_m, 4),
            "pearson_r": None if r is None else round(r, 3),
            "t_stat": None if t is None else round(t, 2),
        })
    return {
        "min_cluster_n": min_n,
        "lumped_into_other": lumped,
        "clusters": out,
        "multiple_testing": _honesty(n_tested, n_hits),
    }


def analyze_card_score(samples: list[Sample]) -> dict:
    """FP-015 deck-level CardScore components vs curator margin.

    A component can be None for some decks ("unavailable != bad"), so each
    feature correlates over only the decks where it computed, with that
    per-feature n reported honestly (r needs n>=3 for a t-stat anyway)."""
    out: list[dict] = []
    n_tested = 0
    n_hits = 0
    for fname in CARD_SCORE_FEATURES:
        xs: list[float] = []
        ys: list[float] = []
        for s in samples:
            v = s.features.get(fname)
            if v is not None:
                xs.append(float(v))
                ys.append(s.margin)
        n_avail = len(xs)
        r = pearson(xs, ys) if n_avail >= 3 else None
        t = t_stat(r, n_avail) if r is not None else None
        if r is not None:
            n_tested += 1
            if t is not None and abs(t) >= 2.0:
                n_hits += 1
        out.append({
            "feature": fname,
            "n_avail": n_avail,
            "pearson_r": None if r is None else round(r, 3),
            "t_stat": None if t is None else round(t, 2),
        })
    out.sort(key=lambda d: abs(d["pearson_r"] or 0.0), reverse=True)
    return {
        "features": out,
        "multiple_testing": _honesty(n_tested, n_hits),
    }


def analyze_tournament(samples: list[Sample]) -> dict:
    """FP-017 cEDH tournament card statistics vs curator margin.

    Same per-feature-n discipline as ``analyze_card_score`` (None means
    "unavailable", never 0), plus an explicit ``n_bracket5`` count: the
    scope gate means a soak made entirely of B3 decks yields n_avail 0
    everywhere, and the report must say WHY rather than looking broken.
    Exploratory by construction -- see the module docstring."""
    n_b5 = sum(1 for s in samples
               if s.features.get("tt_coverage") is not None)
    out: list[dict] = []
    n_tested = 0
    n_hits = 0
    for fname in TOURNAMENT_FEATURES:
        xs: list[float] = []
        ys: list[float] = []
        for s in samples:
            v = s.features.get(fname)
            if v is not None:
                xs.append(float(v))
                ys.append(s.margin)
        n_avail = len(xs)
        r = pearson(xs, ys) if n_avail >= 3 else None
        t = t_stat(r, n_avail) if r is not None else None
        if r is not None:
            n_tested += 1
            if t is not None and abs(t) >= 2.0:
                n_hits += 1
        out.append({
            "feature": fname,
            "n_avail": n_avail,
            "pearson_r": None if r is None else round(r, 3),
            "t_stat": None if t is None else round(t, 2),
        })
    out.sort(key=lambda d: abs(d["pearson_r"] or 0.0), reverse=True)
    return {
        "features": out,
        "n_bracket5_decks": n_b5,
        "n_decks": len(samples),
        "scope_note": (
            "cEDH tournament data describes BRACKET-5 humans only; decks "
            "not tagged [B5] are excluded by design, not by accident. "
            "Exploratory source, not a validated predictor."),
        "multiple_testing": _honesty(n_tested, n_hits),
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
    ap.add_argument("--features", choices=FEATURE_FAMILIES,
                    default="deck_health",
                    help="feature substrate to regress margin on: deck_health "
                         "(default; the original 10 features, byte-identical "
                         "output), clusters (corpus-theme labels, one-vs-rest), "
                         "card_score (FP-015 deck-level components), "
                         "tournament (FP-017 cEDH card stats; BRACKET-5 decks "
                         "only, cache-only, exploratory), or all.")
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
        samples, skipped, missing = build_gauntlet_samples(
            pairs, decks_dirs, features=args.features)
    else:
        rows = load_rows(args.inbox)
        pairs = aggregate_pairs(rows, min_games=args.min_games)
        samples, skipped, missing = build_samples(
            pairs, decks_dirs, features=args.features)
    dh_active = args.features in ("deck_health", "all")
    report = analyze(samples, feature_names=None if dh_active else [])
    report["mode"] = args.mode
    report["skipped"] = skipped
    report["min_games"] = args.min_games
    report["missing_deck_files"] = missing            # {deck: rows dropped}
    report["missing_deck_rows_dropped"] = sum(missing.values())
    if args.features != "deck_health":
        # New-substrate reports only; the default report keys stay pinned
        # byte-identical (tests assert this).
        report["features"] = args.features
        if dh_active:
            fam = report["feature_correlations"]
            report["deck_health_multiple_testing"] = _honesty(
                sum(1 for f in fam if f["pearson_r"] is not None),
                sum(1 for f in fam
                    if f["t_stat"] is not None and abs(f["t_stat"]) >= 2.0))
        if args.features in ("clusters", "all"):
            report["cluster_analysis"] = analyze_clusters(samples)
        if args.features in ("card_score", "all"):
            report["card_score_analysis"] = analyze_card_score(samples)
        if args.features in ("tournament", "all"):
            report["tournament_analysis"] = analyze_tournament(samples)

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    margin_desc = ("winrate(v2)-winrate(base) vs fixed gauntlet"
                   if args.mode == "gauntlet" else "per-deck win-rate delta")
    feat_tag = ("" if args.features == "deck_health"
                else f", features={args.features}")
    print(f"FP-002 margin analysis  (mode={args.mode}, "
          f"min_games={args.min_games}{feat_tag})")
    print(f"  decks: {report['n_decks']}   games: {report['total_games']}")
    print(f"  mean curator margin: {report['mean_margin']:+.4f}  "
          f"(>0 = curation helps; {margin_desc})")
    v = report["verdicts"]
    print(f"  per-deck verdicts: kept={v['kept']}  "
          f"reverted={v['reverted']}  neutral={v['neutral']}")
    if dh_active:
        print(f"\n  feature -> margin correlation (|r| desc, "
              f"n={report['n_decks']} decks):")
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
        if "deck_health_multiple_testing" in report:
            print(_honesty_line("deck_health",
                                report["deck_health_multiple_testing"]))
    if "cluster_analysis" in report:
        ca = report["cluster_analysis"]
        print(f"\n  corpus-theme cluster -> margin (one-vs-rest point-"
              f"biserial; indicator tested at n>={ca['min_cluster_n']}, "
              f"n={report['n_decks']} decks):")
        for c in ca["clusters"]:
            r, t = c["pearson_r"], c["t_stat"]
            if r is not None:
                star = "*" if (t is not None and abs(t) >= 2.0) else " "
                stat = f"r={r:+.3f}  t={t if t is not None else 'NA':>6}  {star}"
            elif c["n"] < ca["min_cluster_n"]:
                stat = "(below min n -- reported, not tested)"
            else:
                stat = "r=  NA   (no variance)"
            print(f"    {c['cluster']:<24} n={c['n']:>3}  "
                  f"mean_margin={c['mean_margin']:+.4f}  {stat}")
        if ca["lumped_into_other"]:
            print(f"    'other' lumps {len(ca['lumped_into_other'])} small "
                  "cluster(s): " + ", ".join(ca["lumped_into_other"]))
        print(_honesty_line("clusters", ca["multiple_testing"]))
    if "card_score_analysis" in report:
        cs = report["card_score_analysis"]
        print("\n  CardScore deck-level component -> margin "
              "(|r| desc; n = decks where the component computed):")
        for f in cs["features"]:
            r, t = f["pearson_r"], f["t_stat"]
            if r is not None:
                star = "*" if (t is not None and abs(t) >= 2.0) else " "
                stat = f"r={r:+.3f}  t={t if t is not None else 'NA':>6}  {star}"
            elif f["n_avail"] == 0:
                stat = "unavailable (computed for 0 decks)"
            else:
                stat = "r=  NA   (no variance or n<3)"
            print(f"    {f['feature']:<24} n={f['n_avail']:>3}  {stat}")
        print(_honesty_line("card_score", cs["multiple_testing"]))
    if "tournament_analysis" in report:
        ta = report["tournament_analysis"]
        print("\n  FP-017 cEDH tournament card stats -> margin "
              f"(bracket-5 decks: {ta['n_bracket5_decks']}/{ta['n_decks']}):")
        if not ta["n_bracket5_decks"]:
            print("    no bracket-5 decks in this sample — every feature is "
                  "unavailable BY DESIGN (cEDH data is gated to B5)")
        for f in ta["features"]:
            r, t = f["pearson_r"], f["t_stat"]
            if r is not None:
                star = "*" if (t is not None and abs(t) >= 2.0) else " "
                stat = f"r={r:+.3f}  t={t if t is not None else 'NA':>6}  {star}"
            elif f["n_avail"] == 0:
                stat = "unavailable (computed for 0 decks)"
            else:
                stat = "r=  NA   (no variance or n<3)"
            print(f"    {f['feature']:<24} n={f['n_avail']:>3}  {stat}")
        print(_honesty_line("tournament", ta["multiple_testing"]))
        print(f"    scope: {ta['scope_note']}")
    if skipped:
        print(f"\n  skipped {len(skipped)} pair(s): "
              + "; ".join(skipped[:6]) + ("..." if len(skipped) > 6 else ""))
    if missing:
        print(f"\n  WARNING: {len(missing)} deck file(s) missing from disk -> "
              f"{report['missing_deck_rows_dropped']} soak row(s) EXCLUDED "
              f"(see stderr for per-file detail)")
    print("\n  note: |t|>=2 (~p<.05, df=n-2) flagged with *. With ~30 decks this "
          "is exploratory.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
