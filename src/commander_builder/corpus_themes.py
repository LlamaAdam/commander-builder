"""Corpus theme mining — how the reference population *normally* builds.

FP-014's assembler borrows coherence from EDHREC's aggregate. This module
adds a second, LOCAL population signal: scan the ~100-200 harvested decks
already on disk (B3 pool harvests, ``[PREMADE]`` popularity pulls, precons,
``[REF]`` imports), derive a structural profile per deck, group the decks
into interpretable archetype clusters, and emit per-cluster *empirical
norms* — median role counts, land counts, curve shape, and signature cards
— as a derived-data JSON artifact.

The user intent, verbatim: "looking at 100 or so decks to see if you can
determine overall themes and work on deck building from seeing how they are
normally built."

HOW THIS RELATES TO THE EXISTING SEAMS (complement, never duplicate):

* ``lift_analysis`` mines PAIRWISE card co-occurrence ("which cards go
  together"). This module mines DECK-LEVEL structure ("how many lands /
  ramp spells / what curve does a tokens deck actually run"). The two are
  orthogonal reads of the same corpus and share the corpus-membership
  conventions (skip ``[USER]``/``[CONTROL]``; staples + basics excluded
  from the card vocabulary).
* ``archetype.py`` classifies a deck into 5 play-pattern labels from card
  NAMES (its own honesty note: coarse, name-based). This module reads
  ORACLE TEXT + type lines from the local Scryfall snapshots, so it can
  see themes names can't carry (aristocrats, blink, landfall...). Its
  labels are build-around THEMES, not play patterns — the two taxonomies
  deliberately coexist.
* ``staples.ROLE_TARGETS`` is the hand-written template. The mined norms
  are the measured population. ``deck_builder`` blends the two 50/50 (see
  ``blended_role_targets``) — conservatively, and only behind the
  ``COMMANDER_BUILDER_CORPUS_NORMS`` flag.
* ``bubble_analysis.build_reference_corpus`` fetches a per-commander
  reference corpus from the NETWORK. This module is strictly offline over
  the decks already harvested to disk.

OFFLINE CONTRACT: card resolution defaults to
``lookup_card(name, cache_only=True)`` — the on-disk oracle snapshots are
the only data source, no network, ever. Cards without a snapshot count
into an honest ``unresolved`` tally and contribute nothing else; a corpus
with no snapshots at all degrades to structure-only profiles (lands via
basic-land names, everything else empty) rather than failing.

CLUSTERING IS TRANSPARENT ON PURPOSE. No ML, no distance metrics — a
first-hit-wins ladder of threshold rules over countable signals (creature-
subtype concentration, oracle-text motif counts, role/curve structure),
pure stdlib, every assignment explainable in one sentence ("18 cards share
the Goblin subtype"). A deck no rule claims lands in the honest
``goodstuff-midrange`` bucket. Determinism: every ladder step breaks ties
by a fixed ordering, so the same corpus always yields the same clusters.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import dck_utils
from .collection import name_key
from .staples import (
    ROLE_TARGETS,
    is_basic_land,
    is_universal_staple,
    role_bucket,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# Versioned like lift_matrix.v1.json — bumping the suffix orphans stale
# artifacts instead of migrating them. data/ (repo root) is the derived-
# data landing zone; the dir is created on first write.
#
# NOTE (2026-08): the ``_facts`` counting fixes (voltron no longer
# double-counts Equipment; DFC subtype parsing is per-face and deduped)
# shift cluster assignment for some decks — regenerating an existing
# artifact with ``commander-corpus-themes`` is recommended so the stored
# norms reflect the corrected counts.
DEFAULT_NORMS_PATH = REPO_ROOT / "data" / "corpus_theme_norms.v1.json"

# Same shape as COMMANDER_BUILDER_CARD_SCORE (FP-015): the builder
# integration is opt-in via env flag so the default `commander-build`
# output is byte-identical with the mined norms absent OR present.
FLAG_ENV = "COMMANDER_BUILDER_CORPUS_NORMS"

# --- Thresholds (each is why-commented; all are deck-count / card-count
# integers so the rules stay explainable) ----------------------------------

# A deck is "tribal-X" when this many nonland cards share creature subtype
# X. 12 ≈ the point where the subtype is a build-around, not incidental
# (lift_analysis' cousin judgement: archetype.py uses 10 for NAME matches;
# subtype counts run a little higher because every typed creature counts).
TRIBAL_MIN = 12
# "Human" is the great false positive — it rides along on a third of all
# creatures ever printed, so it needs a much higher bar to be the THEME.
TRIBAL_MIN_HUMAN = 20

# A motif is the deck's theme when at least this many cards carry it.
# 8 is deliberately below the ~10+ a committed theme deck runs, because
# motif regexes under-count (oracle templating varies) — the same
# under-count honesty that made archetype.py drop its oracle phrases.
MOTIF_MIN = 8

# Type-concentration thresholds. Artifacts need a high bar (every deck
# runs ~6-10 rocks); enchantments less so.
ARTIFACT_MIN = 22
ENCHANTMENT_MIN = 15

# Structural fallbacks (see _classify): big-mana and interaction-pile.
BIG_MANA_CMC = 3.6      # mean CMC above this = top-heavy on purpose...
BIG_MANA_RAMP = 10      # ...but only when the ramp to support it exists.
CONTROL_INTERACTION = 13  # removal+wipe count of a dedicated control pile.
COMBO_TUTORS = 4        # tutor density is the one offline combo tell.

# Signature cards: within-cluster frequency floor, global-lift floor, and
# minimum absolute support (3 decks — the same small-N guard as
# lift_analysis.SUPPORT_FLOOR, for the same variance reason).
SIGNATURE_MIN_DECKS = 3
SIGNATURE_MIN_CLUSTER_FREQ = 0.4
SIGNATURE_MIN_RATIO = 2.0
SIGNATURE_LIMIT = 10

# Norms are only worth steering toward when measured over at least this
# many decks; thinner clusters still appear in the report (labeled) but
# carry no authority in the builder.
MIN_CLUSTER_FOR_NORMS = 3

# Builder integration: bounded swap budget (mirrors DEFAULT_MAX_LIFT_SWAPS'
# "stay recognizably the seed" rationale) and the land-target sanity band.
MAX_NORMS_SWAPS = 4
LAND_TARGET_MIN, LAND_TARGET_MAX = 30, 45

# Curve histogram buckets. "7+" folds the long tail so the shape is
# comparable across decks.
CURVE_BUCKETS = ["0", "1", "2", "3", "4", "5", "6", "7+"]

# The roles whose per-cluster medians the builder may steer toward — the
# ROLE_TARGETS vocabulary exactly, so template and empirical numbers are
# always about the same buckets. (win_condition folds into finisher at
# profile time, same reconciliation as staples.role_target_report.)
NORM_ROLES = list(ROLE_TARGETS.keys())

# --- Theme motif table ----------------------------------------------------
#
# Ordered: earlier themes win ties (fixed order = deterministic output).
# Every pattern matches ORACLE TEXT (lowercased); themes that are really
# TYPE concentrations (artifacts / enchantments / voltron) are counted in
# the facts loop instead, because type lines are the honest signal there.
_THEME_MOTIFS: list[tuple[str, list[re.Pattern[str]]]] = [
    ("tokens", [re.compile(r"create[^.]{0,60}token")]),
    ("aristocrats", [
        re.compile(r"whenever [^.]{0,40}dies"),
        re.compile(r"sacrifice a(?:nother)? creature"),
    ]),
    ("counters", [
        re.compile(r"\+1/\+1 counter"),
        re.compile(r"proliferate"),
    ]),
    ("spellslinger", [
        re.compile(
            r"whenever you cast (?:an instant|a sorcery|an instant or "
            r"sorcery|a noncreature)"
        ),
        re.compile(r"copy target (?:instant|sorcery)"),
        re.compile(r"prowess|magecraft|storm"),
    ]),
    ("lands-matter", [
        re.compile(r"landfall"),
        re.compile(r"whenever a land enters"),
        re.compile(r"play an additional land"),
        re.compile(r"whenever you play a land"),
    ]),
    ("graveyard", [
        re.compile(r"return[^.]{0,80}from your graveyard"),
        re.compile(r"from (?:your|a) graveyard to the battlefield"),
        re.compile(r"\bflashback\b|\bunearth\b|\bescape\b|\bdisturb\b"),
    ]),
    ("mill", [
        re.compile(r"mills? \w+ cards?"),
        re.compile(r"puts? the top [^.]{0,60}into (?:their|your) graveyard"),
    ]),
    ("lifegain", [
        re.compile(r"you gain [^.]{0,20}life"),
        re.compile(r"whenever you gain life"),
        re.compile(r"\blifelink\b"),
    ]),
    ("blink", [
        re.compile(
            r"exile [^.]{0,80}return (?:it|that card|them|those cards)"
            r"[^.]{0,60}battlefield"
        ),
    ]),
    ("voltron", [  # oracle half; the Equipment/Aura type half is in _facts.
        re.compile(r"equipped creature|attach"),
        re.compile(r"enchanted creature gets"),
    ]),
    ("stax", [
        re.compile(r"players? can't"),
        re.compile(r"each (?:player|opponent) sacrifices"),
        re.compile(r"spells? [^.]{0,30}cost [^.]{0,15}more to cast"),
        re.compile(r"don't untap|doesn't untap during"),
    ]),
    ("wheels", [
        re.compile(r"each player discards"),
        re.compile(r"discards? (?:their|his or her) hand"),
    ]),
]

# Motif theme → cluster label. Separate map so labels can read better than
# internal motif keys.
_MOTIF_CLUSTER_LABEL: dict[str, str] = {
    "tokens": "tokens-go-wide",
    "aristocrats": "aristocrats",
    "counters": "plus-one-counters",
    "spellslinger": "spellslinger",
    "lands-matter": "lands-matter",
    "graveyard": "reanimator-graveyard",
    "mill": "mill",
    "lifegain": "lifegain",
    "blink": "blink-flicker",
    "voltron": "voltron-equipment",
    "stax": "stax-control",
    "wheels": "wheel-discard",
}


# --- Corpus membership ----------------------------------------------------

# Filename-prefix role convention shared with lift_analysis / premade_import:
# [USER] = the user's own decks, [CONTROL] = calibration do-nothings,
# [PREMADE] = popularity pulls, [REF] = meta_test imports, anything else =
# harvested pool (which is where the precons live).
_PREFIX_ROLES = (
    ("[USER]", "user"),
    ("[CONTROL]", "control"),
    ("[PREMADE]", "premade"),
    ("[REF]", "ref"),
)
DEFAULT_ROLES = frozenset({"pool", "premade", "ref"})
ALL_ROLES = frozenset({"pool", "premade", "ref", "user", "control"})


def file_role(filename: str) -> str:
    """Corpus role of a deck file, from its filename prefix."""
    for prefix, role in _PREFIX_ROLES:
        if filename.startswith(prefix):
            return role
    return "pool"


def default_lookup(name: str) -> Optional[dict]:
    """Offline-only card resolution: local oracle snapshots, no network."""
    from .scryfall_client import lookup_card
    return lookup_card(name, cache_only=True)


# --- Per-deck profile -----------------------------------------------------


@dataclass
class DeckProfile:
    """One deck reduced to its theme-mining-relevant structure."""

    filename: str
    role: str                      # pool / premade / ref / user / control
    commanders: list[str]
    role_counts: dict[str, int]    # role bucket → nonland card count
    curve: dict[str, int]          # CURVE_BUCKETS histogram (nonland)
    cmc_mean: float
    cmc_median: float
    land_count: int
    color_count: int
    tribes: dict[str, int]         # creature subtype → card count
    motifs: dict[str, int]         # motif theme → card count
    artifact_count: int
    enchantment_count: int
    card_keys: list[str]           # signature vocabulary (see _facts)
    display_names: dict[str, str] = field(default_factory=dict)
    unresolved: int = 0
    cluster: str = ""              # filled by cluster_profiles
    cluster_reason: str = ""


def _facts(entries, lookup, commander_keys: Optional[set[str]] = None) -> dict:
    """Fold ``(qty, name)`` card entries into the raw countable facts.

    Shared by ``profile_deck`` (whole .dck files) and ``classify_shell``
    (the builder's in-memory nonland list) so a shell is classified by
    EXACTLY the rules that clustered the corpus.

    COMMANDER TREATMENT: cards whose key is in ``commander_keys`` count
    toward the THEME signals (tribes / motifs / colors — a tribal or
    lifegain commander is the theme's loudest card) but NOT toward the
    structural counts (role buckets, curve, signature vocabulary): the
    norms describe how the 99 is built, and the command zone is not a
    slot the builder fills.

    Vocabulary rule for ``card_keys`` (the signature-card denominator):
    nonland, non-basic, non-universal-staple — the same exclusions as
    lift_analysis' vocabulary, for the same reason (Sol Ring in every
    deck is fame, not signal).
    """
    commander_keys = commander_keys or set()
    role_counts: Counter = Counter()
    tribes: Counter = Counter()
    motifs: Counter = Counter()
    curve: Counter = Counter()
    cmcs: list[float] = []
    colors: set[str] = set()
    display: dict[str, str] = {}
    card_keys: list[str] = []
    seen_keys: set[str] = set()
    land_count = 0
    artifact_count = 0
    enchantment_count = 0
    unresolved = 0

    for qty, nm in entries:
        key = name_key(nm)
        if not key:
            continue
        if is_basic_land(key):
            land_count += qty
            continue
        try:
            card = lookup(nm)
        except Exception:  # noqa: BLE001 — one bad snapshot ≠ a dead scan.
            card = None
        if not card:
            unresolved += qty
            continue
        type_line = (card.get("type_line") or "").lower()
        oracle = (card.get("oracle_text") or "").lower()
        colors.update(card.get("color_identity") or [])
        if "land" in type_line:
            land_count += qty
            continue
        is_commander = key in commander_keys
        # --- nonland card: structural counts (the 99 only) ----------------
        if not is_commander:
            role_counts[role_bucket(oracle, type_line)] += qty
            cmc = card.get("cmc")
            if isinstance(cmc, (int, float)):
                cmcs.extend([float(cmc)] * qty)
                bucket = "7+" if cmc >= 7 else str(int(cmc))
                curve[bucket] += qty
        if "creature" in type_line:
            # Subtypes follow the em-dash: "Creature — Goblin Warrior".
            # DFCs are handled one FACE at a time (split on "//" first):
            # splitting the whole line on its first dash used to leave the
            # back face's "Creature" card-type token in the subtype list
            # and count subtypes both faces share twice. Faces dedupe into
            # a set so a card contributes each subtype at most once, and
            # only creature faces speak (an MDFC's land face is no tribe).
            subtypes: set[str] = set()
            for face in (card.get("type_line") or "").split("//"):
                if "creature" not in face.lower():
                    continue
                for part in re.split(r"[—-]", face, maxsplit=1)[1:]:
                    for subtype in part.split():
                        if subtype[:1].isupper():
                            subtypes.add(subtype)
            for subtype in sorted(subtypes):
                tribes[subtype] += qty
        if "artifact" in type_line:
            artifact_count += qty
        if "enchantment" in type_line:
            enchantment_count += qty
        # Voltron's two halves — the Equipment/Aura type line here, the
        # "equipped creature ..." oracle patterns in _THEME_MOTIFS — must
        # count a card ONCE: every real Equipment card's own text matches
        # the oracle half too, and counting both silently halved the
        # documented MOTIF_MIN bar (~4 Equipment looked like 8 motif hits).
        is_voltron_type = "equipment" in type_line or "aura" in type_line
        if is_voltron_type:
            motifs["voltron"] += qty
        for theme, patterns in _THEME_MOTIFS:
            if theme == "voltron" and is_voltron_type:
                continue  # already counted via the type line above
            if any(p.search(oracle) for p in patterns):
                motifs[theme] += qty
        if (not is_commander and key not in seen_keys
                and not is_universal_staple(key)):
            seen_keys.add(key)
            card_keys.append(key)
            display[key] = nm.split("//", 1)[0].strip()

    return {
        "role_counts": dict(role_counts),
        "tribes": dict(tribes),
        "motifs": dict(motifs),
        "curve": {b: curve.get(b, 0) for b in CURVE_BUCKETS},
        "cmcs": cmcs,
        "colors": colors,
        "land_count": land_count,
        "artifact_count": artifact_count,
        "enchantment_count": enchantment_count,
        "card_keys": card_keys,
        "display_names": display,
        "unresolved": unresolved,
    }


def _deck_entries(deck_text: str):
    """``(qty, name)`` pairs across [Commander] + [Main]."""
    for section in ("Commander", "Main"):
        for line in dck_utils.iter_section_lines(deck_text, section):
            parsed = dck_utils.parse_card_line(line)
            if parsed is None:
                continue
            qty, nm = parsed
            if nm:
                yield qty, nm


def profile_deck(
    path: Path, lookup: Optional[Callable[[str], Optional[dict]]] = None,
) -> Optional[DeckProfile]:
    """Extract one deck's structural profile. None when unreadable/empty."""
    if lookup is None:
        lookup = default_lookup
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    entries = list(_deck_entries(text))
    if not entries:
        return None
    commanders = dck_utils.section_card_names(text, "Commander")
    facts = _facts(entries, lookup,
                   commander_keys={name_key(c) for c in commanders})
    cmcs = facts["cmcs"]
    # Fold win_condition into finisher — the same taxonomy reconciliation
    # staples.role_target_report performs, so norms and targets agree.
    role_counts = dict(facts["role_counts"])
    if "win_condition" in role_counts:
        role_counts["finisher"] = (
            role_counts.get("finisher", 0) + role_counts.pop("win_condition")
        )
    return DeckProfile(
        filename=Path(path).name,
        role=file_role(Path(path).name),
        commanders=commanders,
        role_counts=role_counts,
        curve=facts["curve"],
        cmc_mean=round(statistics.fmean(cmcs), 2) if cmcs else 0.0,
        cmc_median=round(statistics.median(cmcs), 2) if cmcs else 0.0,
        land_count=facts["land_count"],
        color_count=len(facts["colors"]),
        tribes=facts["tribes"],
        motifs=facts["motifs"],
        artifact_count=facts["artifact_count"],
        enchantment_count=facts["enchantment_count"],
        card_keys=facts["card_keys"],
        display_names=facts["display_names"],
        unresolved=facts["unresolved"],
    )


def scan_corpus(
    deck_dir: Path,
    roles: frozenset[str] = DEFAULT_ROLES,
    lookup: Optional[Callable[[str], Optional[dict]]] = None,
) -> list[DeckProfile]:
    """Profile every corpus-eligible ``*.dck`` under ``deck_dir``.

    ``roles`` filters by filename-prefix role (default: pool + premade +
    ref — everything except the user's own decks and the calibration
    controls, mirroring lift_analysis' corpus definition). Unreadable
    files are skipped, not fatal.
    """
    if not deck_dir or not Path(deck_dir).is_dir():
        return []
    out: list[DeckProfile] = []
    for f in sorted(Path(deck_dir).glob("*.dck")):
        if file_role(f.name) not in roles:
            continue
        profile = profile_deck(f, lookup)
        if profile is not None:
            out.append(profile)
    return out


# --- Clustering (the transparent ladder) ----------------------------------


def _classify(profile: DeckProfile) -> tuple[str, str]:
    """Assign one deck to a cluster. Returns ``(label, reason)``.

    First hit wins; every rung's tie-break is a fixed ordering, so the
    result is order-independent and deterministic. The rungs, in order of
    signal strength:

      1. TRIBAL — a creature subtype concentrated past TRIBAL_MIN is the
         loudest build-around there is. Human needs a higher bar (see the
         threshold comment).
      2. ORACLE MOTIFS — the _THEME_MOTIFS table; strongest count wins,
         ties break by table order.
      3. TYPE CONCENTRATION — artifacts-matter / enchantress, which type
         lines signal more honestly than oracle text.
      4. STRUCTURE — ramp-big-mana (top-heavy curve + the ramp to cast
         it), control-interaction (removal+wipe density), combo-tutors
         (tutor density: the one combo tell visible offline; real combo
         detection needs the combo DB, out of scope here).
      5. goodstuff-midrange — the honest default, never forced.
    """
    # 1. Tribal.
    best_tribe, best_n = "", 0
    for tribe in sorted(profile.tribes):  # fixed order for ties
        n = profile.tribes[tribe]
        floor = TRIBAL_MIN_HUMAN if tribe.lower() == "human" else TRIBAL_MIN
        if n >= floor and n > best_n:
            best_tribe, best_n = tribe, n
    if best_tribe:
        return (
            f"tribal-{best_tribe.lower()}",
            f"{best_n} cards share the {best_tribe} subtype",
        )
    # 2. Oracle motifs (table order breaks ties).
    best_theme, best_n = "", 0
    for theme, _patterns in _THEME_MOTIFS:
        n = profile.motifs.get(theme, 0)
        if n >= MOTIF_MIN and n > best_n:
            best_theme, best_n = theme, n
    if best_theme:
        return (
            _MOTIF_CLUSTER_LABEL[best_theme],
            f"{best_n} cards carry the {best_theme} motif",
        )
    # 3. Type concentration.
    if profile.artifact_count >= ARTIFACT_MIN:
        return (
            "artifacts-matter",
            f"{profile.artifact_count} artifact cards",
        )
    if profile.enchantment_count >= ENCHANTMENT_MIN:
        return (
            "enchantress",
            f"{profile.enchantment_count} enchantment cards",
        )
    # 4. Structure.
    ramp = profile.role_counts.get("ramp", 0)
    if profile.cmc_mean >= BIG_MANA_CMC and ramp >= BIG_MANA_RAMP:
        return (
            "ramp-big-mana",
            f"mean CMC {profile.cmc_mean:.1f} with {ramp} ramp cards",
        )
    interaction = (
        profile.role_counts.get("removal", 0)
        + profile.role_counts.get("wipe", 0)
    )
    if interaction >= CONTROL_INTERACTION:
        return (
            "control-interaction",
            f"{interaction} removal/wipe cards",
        )
    tutors = profile.role_counts.get("tutor", 0)
    if tutors >= COMBO_TUTORS:
        return "combo-tutors", f"{tutors} tutors (offline combo tell)"
    # 5. Default.
    return "goodstuff-midrange", "no dominant theme signal"


def cluster_profiles(profiles: list[DeckProfile]) -> list[DeckProfile]:
    """Classify every profile in place (fills cluster/cluster_reason)."""
    for p in profiles:
        p.cluster, p.cluster_reason = _classify(p)
    return profiles


# --- Norms (the derived-data artifact) ------------------------------------


def _median_int(values: list[int]) -> int:
    return int(round(statistics.median(values))) if values else 0


def compute_norms(profiles: list[DeckProfile], deck_dir: str = "") -> dict:
    """Fold clustered profiles into the per-cluster norms artifact.

    Signature cards = high within-cluster frequency, low global frequency
    (the report's "these cards say tokens deck"): a card kept when it
    appears in ≥ SIGNATURE_MIN_DECKS cluster decks, ≥ 40% of the cluster,
    and its cluster frequency is ≥ 2x its global frequency — the same
    popularity-normalization idea as lift, at deck-cluster granularity.
    """
    for p in profiles:
        if not p.cluster:
            p.cluster, p.cluster_reason = _classify(p)
    n_total = len(profiles)
    global_counts: Counter = Counter()
    names: dict[str, str] = {}
    for p in profiles:
        global_counts.update(p.card_keys)
        for k, v in p.display_names.items():
            names.setdefault(k, v)

    by_cluster: dict[str, list[DeckProfile]] = defaultdict(list)
    for p in profiles:
        by_cluster[p.cluster].append(p)

    clusters: dict[str, dict] = {}
    for label in sorted(by_cluster):
        members = by_cluster[label]
        n = len(members)
        role_medians = {
            role: _median_int([m.role_counts.get(role, 0) for m in members])
            for role in NORM_ROLES
        }
        curve_median = {
            b: _median_int([m.curve.get(b, 0) for m in members])
            for b in CURVE_BUCKETS
        }
        signatures: list[dict] = []
        if n >= SIGNATURE_MIN_DECKS and n_total > 0:
            in_cluster: Counter = Counter()
            for m in members:
                in_cluster.update(m.card_keys)
            for key, c in in_cluster.items():
                if c < SIGNATURE_MIN_DECKS:
                    continue
                cluster_freq = c / n
                global_freq = global_counts[key] / n_total
                if cluster_freq < SIGNATURE_MIN_CLUSTER_FREQ:
                    continue
                if global_freq <= 0:
                    continue
                ratio = cluster_freq / global_freq
                if ratio < SIGNATURE_MIN_RATIO:
                    continue
                signatures.append({
                    "card": names.get(key, key),
                    "cluster_freq": round(cluster_freq, 2),
                    "global_freq": round(global_freq, 2),
                    "ratio": round(ratio, 1),
                })
            signatures.sort(
                key=lambda s: (-s["cluster_freq"], -s["ratio"], s["card"])
            )
            signatures = signatures[:SIGNATURE_LIMIT]
        clusters[label] = {
            "n_decks": n,
            "reason_example": members[0].cluster_reason,
            "examples": [m.filename for m in members[:5]],
            "role_medians": role_medians,
            "land_median": _median_int([m.land_count for m in members]),
            "cmc_mean_median": round(
                statistics.median([m.cmc_mean for m in members]), 2
            ),
            "color_count_median": _median_int(
                [m.color_count for m in members]
            ),
            "curve_median": curve_median,
            "signature_cards": signatures,
        }

    return {
        "version": 1,
        "deck_dir": str(deck_dir),
        "n_decks": n_total,
        "roles": NORM_ROLES,
        "clusters": clusters,
    }


def write_norms(norms: dict, path: Optional[Path] = None) -> Path:
    """Write the artifact JSON (creating data/ as needed); returns path."""
    path = Path(path) if path else DEFAULT_NORMS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(norms, indent=2), encoding="utf-8")
    return path


def load_norms(path: Optional[Path] = None) -> Optional[dict]:
    """Load a norms artifact; None (clean no-op) when absent/corrupt."""
    path = Path(path) if path else DEFAULT_NORMS_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(
        data.get("clusters"), dict
    ):
        return None
    return data


# --- Builder integration (FP-014, flag-gated) -----------------------------


def is_enabled() -> bool:
    """Env-flag gate, read at CALL time (the DEFAULT_DB_PATH lesson)."""
    return os.environ.get(FLAG_ENV, "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def classify_shell(
    commanders: list[str],
    nonlands: list[str],
    lookup: Optional[Callable[[str], Optional[dict]]] = None,
) -> tuple[str, str]:
    """Classify the builder's in-memory shell with the corpus ladder.

    Same ``_facts`` + ``_classify`` machinery as ``profile_deck``, so a
    shell matches a cluster by exactly the rules that formed it. The
    commanders join the facts (a tribal commander's own subtype counts —
    it IS the theme's loudest card).
    """
    if lookup is None:
        lookup = default_lookup
    entries = [(1, nm) for nm in list(commanders) + list(nonlands)]
    facts = _facts(entries, lookup,
                   commander_keys={name_key(c) for c in commanders})
    cmcs = facts["cmcs"]
    role_counts = dict(facts["role_counts"])
    if "win_condition" in role_counts:
        role_counts["finisher"] = (
            role_counts.get("finisher", 0) + role_counts.pop("win_condition")
        )
    pseudo = DeckProfile(
        filename="<shell>", role="user", commanders=list(commanders),
        role_counts=role_counts,
        curve=facts["curve"],
        cmc_mean=round(statistics.fmean(cmcs), 2) if cmcs else 0.0,
        cmc_median=round(statistics.median(cmcs), 2) if cmcs else 0.0,
        land_count=facts["land_count"],
        color_count=len(facts["colors"]),
        tribes=facts["tribes"], motifs=facts["motifs"],
        artifact_count=facts["artifact_count"],
        enchantment_count=facts["enchantment_count"],
        card_keys=facts["card_keys"],
    )
    return _classify(pseudo)


def cluster_for_shell(
    norms: Optional[dict],
    commanders: list[str],
    nonlands: list[str],
    lookup: Optional[Callable[[str], Optional[dict]]] = None,
) -> tuple[Optional[str], Optional[dict]]:
    """Match the shell to a MEASURED cluster. ``(label, cluster)`` or
    ``(None, None)``.

    A match only carries authority when the mined cluster holds at least
    MIN_CLUSTER_FOR_NORMS decks — steering toward a 1-deck "norm" would
    be the small-N garbage every corpus module here refuses to emit. The
    goodstuff-midrange default is also excluded: it is the ABSENCE of a
    theme, and its "norms" are just the population average the template
    already encodes.
    """
    if not norms:
        return None, None
    label, _reason = classify_shell(commanders, nonlands, lookup)
    if label == "goodstuff-midrange":
        return None, None
    cluster = norms.get("clusters", {}).get(label)
    if not cluster or cluster.get("n_decks", 0) < MIN_CLUSTER_FOR_NORMS:
        return None, None
    return label, cluster


def blended_role_targets(cluster: dict) -> dict[str, int]:
    """Template ⊕ empirical role targets — the 50/50 blend.

    WHY A BLEND, NOT A REPLACEMENT: the hand-written ROLE_TARGETS encode
    format wisdom (a deck below 8 removal loses to the table no matter
    what its cluster runs); the medians encode what this population
    actually builds. Averaging keeps both authorities honest — mined
    norms can pull a target a few slots, never rewrite it. Rounding is
    half-up via ``round`` on the mean of two ints.
    """
    medians = cluster.get("role_medians", {})
    out: dict[str, int] = {}
    for role, template in ROLE_TARGETS.items():
        emp = medians.get(role)
        if isinstance(emp, (int, float)):
            out[role] = int(round((template + emp) / 2))
        else:
            out[role] = template
    return out


def blended_land_target(computed: int, cluster: dict) -> int:
    """Blend the curve-model land count with the cluster's median.

    Same 50/50 rationale as the role targets; clamped to the sane
    Commander band so a degenerate cluster median can't produce an
    unplayable manabase.
    """
    emp = cluster.get("land_median")
    if not isinstance(emp, (int, float)) or emp <= 0:
        return computed
    blended = int(round((computed + emp) / 2))
    return max(LAND_TARGET_MIN, min(LAND_TARGET_MAX, blended))


def norms_steer(
    nonlands: list[str],
    *,
    label: str,
    cluster: dict,
    role_of: Callable[[str], str],
    ci_ok: Callable[[str], bool],
    reserved_keys: set[str],
    mv_of: Callable[[str], Optional[float]],
    max_swaps: int = MAX_NORMS_SWAPS,
) -> tuple[list[str], list[str]]:
    """Nudge the shell's role mix toward the cluster's blended targets.

    TAXONOMY CONTRACT: ``role_of`` must speak the SAME taxonomy the
    cluster medians were computed with — ``staples.role_bucket`` (base
    taxonomy; the ``win_condition`` → ``finisher`` fold happens here,
    mirroring ``profile_deck``). An extended-taxonomy classifier is a
    bug, not an option: ``classify_role_extended`` files lands-matter
    payoffs under a ``land_payoff`` bucket the corpus side doesn't have,
    so the shell reads phantom draw deficits and the untargeted
    ``land_payoff`` bucket becomes the preferred eviction donor —
    exactly the on-theme cards the steer must protect.

    Net-zero swap engine, same invariant contract as the FP-014.3 stages
    (deck_builder re-validates via ``_revalidate_swaps`` regardless):

      * ADD candidates come from the cluster's SIGNATURE CARDS — the
        cards this population's decks of this theme actually run. That
        is the point of the mining: the empirical answer to "what do I
        put in the extra draw slot" is "what tokens decks normally run",
        not a generic staple.
      * A candidate is only added when its role bucket is BELOW the
        blended target (never past it), and it displaces a card from
        the most OVER-represented donor role (untargeted buckets —
        threat/other/unknown — always count as donatable surplus).
      * CURVE NUDGE: among the donor role's cards, the one whose mana
        value sits farthest from the cluster's median mean-CMC goes
        first — each swap moves the curve toward the population shape.

    Bounded by ``max_swaps``; returns ``(new_nonlands, notes)`` and the
    input unchanged when there is nothing actionable (absent signature
    list, no deficits, no donors) — the clean no-op the caller relies on.
    """
    signatures = [s.get("card", "") for s in cluster.get("signature_cards", [])]
    signatures = [s for s in signatures if s]
    if not signatures:
        return nonlands, []
    targets = blended_role_targets(cluster)
    cmc_anchor = cluster.get("cmc_mean_median")
    if not isinstance(cmc_anchor, (int, float)):
        cmc_anchor = 3.0

    def bucket(nm: str) -> str:
        r = role_of(nm)
        return "finisher" if r == "win_condition" else r

    working = list(nonlands)
    counts: Counter = Counter(bucket(n) for n in working)
    notes: list[str] = []

    def donor_role() -> Optional[str]:
        # Most surplus first; untargeted buckets carry infinite surplus in
        # spirit — rank them by raw count. Fixed sort keys keep it
        # deterministic.
        best, best_surplus = None, 0
        for r, c in sorted(counts.items()):
            surplus = c - targets[r] if r in targets else c
            if surplus > best_surplus:
                best, best_surplus = r, surplus
        return best

    for cand in signatures:
        if len(notes) >= max_swaps:
            break
        ck = name_key(cand)
        live = {name_key(n) for n in working}
        if ck in reserved_keys or ck in live:
            continue
        if not ci_ok(cand):
            continue
        cand_role = bucket(cand)
        target = targets.get(cand_role)
        if target is None or counts.get(cand_role, 0) >= target:
            continue  # only fill measured deficits, never overshoot.
        donor = donor_role()
        if donor is None or donor == cand_role:
            continue
        donor_cards = [n for n in working if bucket(n) == donor]
        if not donor_cards:
            continue
        # Curve nudge: shed the donor card farthest from the population
        # curve anchor (ties: latest position = lowest seed priority).
        def _dist(nm: str) -> float:
            mv = mv_of(nm)
            return abs(mv - cmc_anchor) if mv is not None else 0.0
        out_name = max(donor_cards, key=lambda nm: (_dist(nm),
                                                    working.index(nm)))
        working[working.index(out_name)] = cand
        counts[donor] -= 1
        counts[cand_role] += 1
        notes.append(
            f"corpus norms ({label}): swapped {out_name} ({donor}) for "
            f"signature card {cand} ({cand_role}) toward cluster median "
            f"{cluster.get('role_medians', {}).get(cand_role, '?')}"
        )

    return working, notes


# --- Report ---------------------------------------------------------------


def format_report(norms: dict) -> str:
    """Human-readable "how they are normally built" summary."""
    lines = [
        "-" * 72,
        f" Corpus theme mining — {norms.get('n_decks', 0)} decks"
        + (f" from {norms['deck_dir']}" if norms.get("deck_dir") else ""),
        "-" * 72,
    ]
    clusters = norms.get("clusters", {})
    if not clusters:
        lines.append("No decks profiled — nothing to report.")
        return "\n".join(lines)
    order = sorted(
        clusters, key=lambda k: (-clusters[k]["n_decks"], k),
    )
    lines.append("")
    lines.append(f"{'cluster':<24}{'decks':>6}{'lands':>7}{'mCMC':>6}  roles "
                 f"(ramp/draw/removal/wipe/prot/finisher)")
    for label in order:
        c = clusters[label]
        rm = c["role_medians"]
        roles = "/".join(str(rm.get(r, 0)) for r in (
            "ramp", "draw", "removal", "wipe", "protection", "finisher"))
        lines.append(
            f"{label:<24}{c['n_decks']:>6}{c['land_median']:>7}"
            f"{c['cmc_mean_median']:>6.1f}  {roles}"
        )
    for label in order:
        c = clusters[label]
        lines.append("")
        lines.append(f"== {label} ({c['n_decks']} decks) ==")
        lines.append(f"  why (example): {c.get('reason_example', '')}")
        lines.append(
            f"  norms: {c['land_median']} lands, mean CMC "
            f"{c['cmc_mean_median']:.1f}, "
            f"{c['color_count_median']} colors; curve "
            + " ".join(f"{b}:{c['curve_median'].get(b, 0)}"
                       for b in CURVE_BUCKETS)
        )
        lines.append("  examples: " + ", ".join(c.get("examples", [])))
        sigs = c.get("signature_cards", [])
        if sigs:
            lines.append("  signature cards (cluster% vs global%):")
            for s in sigs:
                lines.append(
                    f"    {s['card']} ({s['cluster_freq']:.0%} vs "
                    f"{s['global_freq']:.0%}, x{s['ratio']:.1f})"
                )
        else:
            lines.append("  signature cards: (none cleared the bar)")
    return "\n".join(lines)


# --- CLI ------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    """``commander-corpus-themes`` — mine the on-disk deck corpus.

    Scans the deck dir, clusters, writes the norms artifact (unless
    ``--no-write``), and optionally prints the human report. Strictly
    offline: only local .dck files + oracle snapshots are read.
    """
    p = argparse.ArgumentParser(
        prog="commander-corpus-themes",
        description=(
            "Mine the harvested deck corpus for archetype clusters and "
            "per-cluster build norms (role counts, curve, lands, "
            "signature cards). Offline; writes "
            "data/corpus_theme_norms.v1.json for commander-build's "
            f"corpus-norms steering (env {FLAG_ENV}=1)."
        ),
    )
    p.add_argument("--deck-dir", type=Path, default=None, metavar="PATH",
                   help="Deck directory to scan (default: the project's "
                        "commander deck dir).")
    p.add_argument("--roles", default="pool,premade,ref", metavar="LIST",
                   help="Comma list of corpus roles to include: "
                        "pool,premade,ref,user,control "
                        "(default pool,premade,ref — [USER]/[CONTROL] "
                        "are skipped).")
    p.add_argument("--out", type=Path, default=None, metavar="PATH",
                   help=f"Artifact path (default {DEFAULT_NORMS_PATH}).")
    p.add_argument("--no-write", action="store_true",
                   help="Analyze only; do not write the artifact.")
    p.add_argument("--report", action="store_true",
                   help="Print the human-readable cluster/norms report.")
    p.add_argument("--json", action="store_true",
                   help="Print the norms artifact JSON to stdout.")
    args = p.parse_args(argv)

    roles = frozenset(
        r.strip().lower() for r in args.roles.split(",") if r.strip()
    )
    bad = roles - ALL_ROLES
    if bad:
        p.error(f"unknown roles: {', '.join(sorted(bad))} "
                f"(valid: {', '.join(sorted(ALL_ROLES))})")

    if args.deck_dir is not None:
        deck_dir = args.deck_dir
    else:
        from .moxfield_import import DECK_OUT_DIR  # lazy: heavy module.
        deck_dir = DECK_OUT_DIR

    profiles = scan_corpus(deck_dir, roles=roles)
    if not profiles:
        print(f"No corpus decks found under {deck_dir} "
              f"(roles: {', '.join(sorted(roles))}).")
        return 1
    cluster_profiles(profiles)
    norms = compute_norms(profiles, deck_dir=str(deck_dir))

    unresolved = sum(pr.unresolved for pr in profiles)
    if unresolved:
        print(f"note: {unresolved} card(s) had no local oracle snapshot "
              f"and were skipped (run online tooling to snapshot them).")

    if not args.no_write:
        out = write_norms(norms, args.out)
        print(f"Wrote {out} ({norms['n_decks']} decks, "
              f"{len(norms['clusters'])} clusters).")
    if args.json:
        print(json.dumps(norms, indent=2))
    if args.report:
        print(format_report(norms))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
