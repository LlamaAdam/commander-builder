"""Popularity-ranked `[PREMADE]` deck importers.

Two pulls, one role. ``commander-import --premade`` fetches the most
popular community builds from both aggregation sites and writes them as
``[PREMADE] <name> [B<n>].dck`` files next to the [USER]/pool decks:

  - Moxfield: the top public Commander decks ranked by LIKES
    (``import_moxfield_premades``). The like count is recorded in the
    deck's ``[metadata]`` as ``Likes=<n>``.
  - EDHREC: the average decks of the most popular COMMANDERS
    (``import_edhrec_premades`` — EDHREC hosts aggregate data, not user
    decks, so "a popular EDHREC deck" means "the average deck of a
    popular commander"). The commander's EDHREC salt score (0-5, from
    the ``/top/salt`` list — commanders appear there as cards) is
    recorded as ``Salt=<x.xx>``.

Both record ``Source=moxfield`` / ``Source=edhrec`` so the provenance
survives in the file. Forge ignores unknown metadata keys (same verified
precedent as ``Moxfield=`` / ``Protect=``).

ROLE SEMANTICS — why `[PREMADE]` is a third role, not pool:

  - Web deck list: premades APPEAR (``web.app._list_decks`` types them
    ``"premade"``) so the user can open/inspect them like any deck.
  - Opponent/filler selection: premades are EXCLUDED
    (``run_match._fallback_opponents``, ``pool_curator``,
    ``_proposer_sim``) — decks selected BY popularity would skew pod
    opposition strength upward.
  - [USER]-keyed scanners (soak pairing, status listing, test-deck
    pickers) never match them — those key on the `[USER]` prefix.
  - Same-id matching stays within the role: a premade copy of a Moxfield
    id never blocks a pool harvest or a user import of the same deck
    (see ``moxfield_import._deck_role`` / ``_existing_moxfield_ids``).

COMMANDER DIVERSITY — both importers skip candidates whose commander is
already represented, so "pull N" means N decks with N distinct
commanders that add something new. The dedupe scope is:

  1. ON-DISK: every commander of every ``.dck`` already in ``out_dir``,
     ANY role ([USER]/[PREMADE]/pool/[REF]/[CONTROL]) — seeded via
     ``existing_commander_names``.
  2. INTRA-PULL: commanders written earlier in the same pull (both
     sources share one ``taken`` set in ``run_premade_pull``, so the
     Moxfield leg's picks also block the EDHREC leg).

Skipped candidates are backfilled by walking further down the
popularity ranking. Names are normalized front-face + lowercase
(``_norm_commander``) on both sides; a multi-commander candidate
(partners) is skipped when ANY of its commanders is already taken.

Bracket tagging follows the declared bracket when Moxfield provides one
(``resolve_bracket``), else the offline ``bracket_estimator``. EDHREC
average decks carry no declared bracket, so they always estimate.

The CLI never runs from here directly — ``commander-import --premade``
(``moxfield_import.main``) calls ``run_premade_pull``.
"""

from __future__ import annotations

import re
import time
import urllib.parse
from pathlib import Path
from typing import Optional

from . import edhrec_client as _edhrec
from . import moxfield_import as _mox
from .bracket_estimator import estimate_bracket
from .dck_meta import stamp_name_preserving_display
from .dck_utils import (
    COMMANDER_DECK_SIZE,
    count_commander_cards,
    parse_card_line,
    section_card_names,
)
from .moxfield_import import DECK_OUT_DIR, FETCH_SLEEP_SEC
from .scryfall_client import lookup_card

PREMADE_PREFIX = _mox._PREMADE_PREFIX


def _norm_commander(name: str) -> str:
    """Normalize a commander name for diversity matching.

    Front face only (EDHREC's convention for DFCs — mirrors
    ``edhrec_client.commander_slug``) and lowercase, so
    "Sephiroth, Fabled SOLDIER // Sephiroth, One-Winged Angel" and
    "sephiroth, fabled soldier" collide on purpose."""
    return name.split("//")[0].strip().lower()


def existing_commander_names(out_dir: Path = DECK_OUT_DIR) -> set[str]:
    """Normalized commander names of every ``.dck`` in ``out_dir``.

    ALL roles on purpose — [USER], [PREMADE], pool, [REF], [CONTROL]:
    this seeds the diversity filter, and a commander the user already
    plays (or the harvest already holds) is "represented" no matter
    which side of the role boundary its deck lives on. Unreadable files
    are skipped (best-effort scan, same tolerance as
    ``moxfield_import._read_moxfield_id``)."""
    taken: set[str] = set()
    if not out_dir.is_dir():
        return taken
    for path in out_dir.glob("*.dck"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for name in section_card_names(text, "Commander"):
            taken.add(_norm_commander(name))
    return taken


def _likes_of(entry: dict) -> int:
    """Best-effort like count from a Moxfield search row / deck JSON."""
    for key in ("likeCount", "likes"):
        v = entry.get(key)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


def _search_top_liked(page_size: int = 50, page: int = 1) -> list[dict]:
    """One page of Moxfield's most-liked public Commander decks.

    Same search endpoint the bracket harvest uses (``SEARCH_BASE``),
    minus the bracket filter — the premade pull ranks globally by
    likes. Server-side likes-desc ordering is requested but re-enforced
    client-side by the caller (Moxfield's sort params have shifted
    between endpoint versions)."""
    params = {
        "pageNumber": str(page),
        "pageSize": str(page_size),
        "sortType": "likes",
        "sortDirection": "descending",
        "fmt": "commander",
    }
    url = f"{_mox.SEARCH_BASE}?{urllib.parse.urlencode(params)}"
    return _mox._http_get_json(url).get("data", [])


def _existing_premade_ids(out_dir: Path) -> dict[str, Path]:
    """Map recorded ``Moxfield=`` id → path for `[PREMADE]` files only.

    The premade-role sibling of ``moxfield_import._existing_moxfield_ids``
    (whose is_user=True/False scopes cover the user and pool roles).
    Premades never version-snapshot, so no lineage resolution is needed;
    a duplicate id keeps the first sorted path."""
    out: dict[str, Path] = {}
    if not out_dir.is_dir():
        return out
    for path in sorted(out_dir.glob("*.dck")):
        if _mox._deck_role(path) != "premade":
            continue
        pid = _mox._read_moxfield_id(path)
        if pid is not None and pid not in out:
            out[pid] = path
    return out


def _premade_destination(deck_name: str, bracket: int, out_dir: Path) -> Path:
    """`[PREMADE] <safe name> [B<n>].dck` — deck_destination's shape with
    the premade role prefix (bracket 0/unknown falls back to `[B?]`,
    matching the user/pool convention)."""
    bracket_suffix = f" [B{bracket}]" if bracket else " [B?]"
    return out_dir / (
        f"{PREMADE_PREFIX} {_mox.safe_filename(deck_name)}{bracket_suffix}.dck"
    )


def _insert_metadata_lines(dck_text: str, extra: list[str]) -> str:
    """Insert ``Key=Value`` lines into the ``[metadata]`` block.

    Placement mirrors ``moxfield_import._merge_local_metadata``: right
    before the first card-section header after ``[metadata]``, so the
    lines stay inside the block every metadata parser reads."""
    if not extra:
        return dck_text
    lines = dck_text.splitlines()
    insert_at = len(lines)
    for i, ln in enumerate(lines):
        if i > 0 and ln.startswith("["):
            insert_at = i
            break
    lines[insert_at:insert_at] = extra
    return "\n".join(lines) + "\n"


def _deck_json_commanders(deck_json: dict) -> list[str]:
    """Normalized commander names from a Moxfield-shape deck JSON."""
    cards = (
        deck_json.get("boards", {}).get("commanders", {}).get("cards", {})
    )
    out: list[str] = []
    for entry in cards.values():
        name = (entry.get("card") or {}).get("name") or ""
        if name:
            out.append(_norm_commander(name))
    return out


def import_moxfield_premades(
    count: int = 10,
    out_dir: Path = DECK_OUT_DIR,
    max_pages: int = 6,
    sleep_sec: float = FETCH_SLEEP_SEC,
    taken_commanders: Optional[set[str]] = None,
) -> list[dict]:
    """Pull the ``count`` most-liked Moxfield Commander decks as premades.

    Selection walks the likes ranking top-down (client-side re-sort per
    page — see ``_search_top_liked``) and SKIPS any deck whose commander
    is already represented, per the module-level COMMANDER DIVERSITY
    contract: on-disk commanders (any role) plus earlier picks in this
    pull. ``taken_commanders`` is mutated in place so a caller can share
    one set across both importer legs; None seeds it from
    ``existing_commander_names(out_dir)``.

    Each written file carries ``Source=moxfield`` + ``Likes=<n>`` in its
    metadata and is bracket-tagged from the deck's declared Moxfield
    bracket, else the offline ``bracket_estimator``. A premade already
    recording the same Moxfield id is overwritten in place (re-pull
    refresh, bracket-drift rename included) — its commander still counts
    as a pick. Returns one summary row per written deck:
    ``{name, source, metric_label, metric_value, bracket, path}``."""
    rows: list[dict] = []
    if count <= 0:
        return rows
    taken = (
        taken_commanders if taken_commanders is not None
        else existing_commander_names(out_dir)
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    id_map = _existing_premade_ids(out_dir)
    seen_ids: set[str] = set()
    print(f"Searching Moxfield for the top {count} decks by likes...")
    page = 1
    while len(rows) < count and page <= max_pages:
        try:
            results = _search_top_liked(page=page)
        except Exception as exc:  # noqa: BLE001 — mirror import_by_bracket
            print(f"  ERROR searching page {page}: {type(exc).__name__}: {exc}")
            page += 1
            continue
        if not results:
            break
        for entry in sorted(results, key=_likes_of, reverse=True):
            if len(rows) >= count:
                break
            pid = entry.get("publicId") or entry.get("id")
            if not pid or pid in seen_ids:
                continue
            seen_ids.add(pid)
            # Diversity pre-check on the search row — skip BEFORE the
            # per-deck fetch when the row already names its commanders.
            row_cmdrs = [
                _norm_commander(c.get("name") or "")
                for c in entry.get("commanders") or []
                if c.get("name")
            ]
            if row_cmdrs and any(c in taken for c in row_cmdrs):
                continue
            try:
                deck_json = _mox.fetch_deck(pid)
            except Exception as exc:  # noqa: BLE001
                print(f"  ERROR fetching {pid}: {type(exc).__name__}: {exc}")
                continue
            finally:
                # Politeness on both paths (import_by_bracket precedent).
                time.sleep(sleep_sec)
            # Authoritative diversity check from the fetched deck JSON —
            # search rows occasionally omit/mislabel commanders.
            cmdrs = _deck_json_commanders(deck_json)
            if any(c in taken for c in cmdrs):
                continue
            likes = _likes_of(deck_json) or _likes_of(entry)
            dck = _mox.to_dck(deck_json)
            bracket = _mox.resolve_bracket(deck_json)
            if not 1 <= bracket <= 5:
                bracket = estimate_bracket(dck)["estimate"]
            dck = _insert_metadata_lines(
                dck, ["Source=moxfield", f"Likes={likes}"],
            )
            same_path = id_map.get(pid)
            if same_path is not None:
                # Same premade already on disk — refresh in place, with
                # the filename's bracket tag kept honest.
                dest = _mox._rename_for_bracket_drift(
                    same_path, bracket, id_map=id_map,
                )
            else:
                dest = _premade_destination(
                    deck_json.get("name", pid), bracket, out_dir,
                )
                if dest.exists():
                    # A DIFFERENT deck owns this sanitized name.
                    dest = _mox._uniquify(dest)
            dck = stamp_name_preserving_display(dck, dest.stem)
            dest.write_text(dck, encoding="utf-8")
            id_map[pid] = dest
            taken.update(cmdrs)
            print(f"  Wrote {dest.name} ({likes} likes, bracket {bracket})")
            rows.append({
                "name": dest.stem,
                "source": "moxfield",
                "metric_label": "likes",
                "metric_value": likes,
                "bracket": bracket,
                "path": str(dest),
            })
        page += 1
    if len(rows) < count:
        print(f"  WARN: only got {len(rows)} of {count} Moxfield premades.")
    return rows


def _salt_for(commander_name: str, salt_map: dict[str, float]) -> float:
    """Commander's salt score from the /top/salt map (0.0 when absent —
    most commanders never make the salt list; absence means 'not salty',
    not 'unknown')."""
    for key in (commander_name.lower(), _norm_commander(commander_name)):
        v = salt_map.get(key)
        if v is not None:
            return float(v)
    return 0.0


def _commander_card_names(commander_name: str) -> list[str]:
    """Resolve an EDHREC commander entry into ``[Commander]`` card names.

    EDHREC joins partner PAIRS with ``//`` — the very same separator a
    single double-faced card's two faces use ("Frodo, Adventurous Hobbit
    // Sam, Loyal Attendant" is TWO cards; "Sephiroth, Fabled SOLDIER //
    Sephiroth, One-Winged Angel" is ONE). Disambiguate with a Scryfall
    exact-name lookup (cached; the importer is a network path anyway):

    - no ``//``                       → single commander, as-is;
    - the full string names one card  → single DFC commander, full name
      (Forge accepts the full "Front // Back" form on a card line);
    - every half names its own card   → partner pair, one line each;
    - lookups inconclusive/offline    → treat as a single commander (the
      conservative shape: still yields a valid 99+1 deck).
    """
    name = (commander_name or "").strip()
    if "//" not in name:
        return [name] if name else []
    if lookup_card(name) is not None:
        return [name]
    halves = [h.strip() for h in name.split("//") if h.strip()]
    if len(halves) >= 2 and all(lookup_card(h) is not None for h in halves):
        return halves
    return [name]


def import_edhrec_premades(
    count: int = 10,
    out_dir: Path = DECK_OUT_DIR,
    taken_commanders: Optional[set[str]] = None,
) -> list[dict]:
    """Build premades from EDHREC's most popular commanders.

    EDHREC hosts aggregate decks, not user decks, so the "top 10 decks"
    are the average decks of the top ``count`` commanders by deck count
    (``fetch_top_commanders``). Selection walks the popularity ranking
    top-down and SKIPS commanders already represented per the
    module-level COMMANDER DIVERSITY contract (on-disk any-role +
    earlier picks in this pull; ``taken_commanders`` shared/mutated
    exactly like ``import_moxfield_premades``).

    Each written file carries ``Source=edhrec`` + ``Salt=<x.xx>`` (the
    commander's score from ``fetch_salt_list``; 0.00 when the commander
    isn't on the salt list) and is bracket-tagged via the offline
    ``bracket_estimator`` (EDHREC declares no bracket for plain average
    decks). Re-pulls overwrite the same commander's premade in place
    (bracket-drift rename included). Returns the same summary-row shape
    as ``import_moxfield_premades``."""
    rows: list[dict] = []
    if count <= 0:
        return rows
    taken = (
        taken_commanders if taken_commanders is not None
        else existing_commander_names(out_dir)
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Fetching EDHREC's top commanders (want {count} average decks)...")
    commanders = _edhrec.fetch_top_commanders()
    if not commanders:
        print("  WARN: EDHREC top-commanders list unavailable; nothing pulled.")
        return rows
    salt_map = _edhrec.fetch_salt_list()
    for c in commanders:
        if len(rows) >= count:
            break
        name = c.name
        if not name or _norm_commander(name) in taken:
            continue
        deck = _edhrec.fetch_average_deck(name)
        if deck is None or not deck.cards:
            print(f"  SKIP {name} (no average deck published/parseable)")
            continue
        # Explicit commander names (partner-aware): the average-deck
        # payload does NOT include the commander, so the shape must
        # inject it — see AverageDeck.to_moxfield_shape.
        dck = _mox.to_dck(deck.to_moxfield_shape(
            commander_names=_commander_card_names(name),
        ))
        bracket = estimate_bracket(dck)["estimate"]
        salt = _salt_for(name, salt_map)
        dck = _insert_metadata_lines(
            dck, ["Source=edhrec", f"Salt={salt:.2f}"],
        )
        dest = _premade_destination(f"EDHREC {name}", bracket, out_dir)
        existing = _find_premade_by_stem_core(out_dir, dest)
        if existing is not None:
            # Same commander's premade already on disk — refresh it in
            # place; the drift rename keeps the [B<n>] tag honest when
            # the estimate moved since the last pull.
            dest = _mox._rename_for_bracket_drift(existing, bracket)
        dck = stamp_name_preserving_display(dck, dest.stem)
        dest.write_text(dck, encoding="utf-8")
        taken.add(_norm_commander(name))
        print(f"  Wrote {dest.name} (salt {salt:.2f}, bracket {bracket})")
        rows.append({
            "name": dest.stem,
            "source": "edhrec",
            "metric_label": "salt",
            "metric_value": salt,
            "bracket": bracket,
            "path": str(dest),
        })
    if len(rows) < count:
        print(f"  WARN: only got {len(rows)} of {count} EDHREC premades.")
    return rows


def _find_premade_by_stem_core(out_dir: Path, dest: Path) -> Optional[Path]:
    """Find an existing `[PREMADE]` file matching ``dest``'s stem minus
    the ` [B<n>]` bracket tag. EDHREC premades have no Moxfield id, so
    the tag-stripped stem is their re-pull identity — a bracket-estimate
    drift must land on the SAME file, not mint a sibling."""
    m = _mox._BRACKET_TAG_STEM.match(dest.stem)
    core = m.group("base") if m else dest.stem
    for p in sorted(out_dir.glob("*.dck")):
        if _mox._deck_role(p) != "premade":
            continue
        pm = _mox._BRACKET_TAG_STEM.match(p.stem)
        if (pm.group("base") if pm else p.stem) == core:
            return p
    return None


def run_premade_pull(
    moxfield_count: int = 10,
    edhrec_count: int = 10,
    out_dir: Path = DECK_OUT_DIR,
) -> int:
    """Run both premade pulls and print a summary table.

    One shared ``taken`` set covers the whole pull, so the two legs
    never duplicate a commander between them (and neither duplicates a
    commander already on disk in any role). Returns 0 when at least one
    deck was written (or nothing was requested), 1 when both legs came
    back empty — ``moxfield_import.main`` counts that as a failure."""
    taken = existing_commander_names(out_dir)
    rows = import_moxfield_premades(
        moxfield_count, out_dir, taken_commanders=taken,
    )
    rows += import_edhrec_premades(
        edhrec_count, out_dir, taken_commanders=taken,
    )

    print(f"\n=== Premade pull summary ({len(rows)} decks) ===")
    if rows:
        name_w = max(len(r["name"]) for r in rows)
        header = f"  {'Deck':<{name_w}}  Source    Likes/Salt  Bracket"
        print(header)
        print("  " + "-" * (len(header) - 2))
        for r in rows:
            metric = (
                f"likes={r['metric_value']}" if r["metric_label"] == "likes"
                else f"salt={r['metric_value']:.2f}"
            )
            print(
                f"  {r['name']:<{name_w}}  {r['source']:<8}  "
                f"{metric:<10}  B{r['bracket']}"
            )
    else:
        print("  (nothing written)")
    if not rows and (moxfield_count > 0 or edhrec_count > 0):
        return 1
    return 0


# ---------------------------------------------------------------------------
# Repair — retrofit [Commander] sections onto broken EDHREC premades
# ---------------------------------------------------------------------------
#
# Every EDHREC premade written before the to_moxfield_shape commander-inject
# fix has NO [Commander] section (the average-deck payload never lists the
# commander, and the old code only routed payload matches). Those files fail
# improvement_advisor's commander parse ("no commanders found") and the
# tier-3 harness. `commander-import --premade-repair` fixes them in place.

# DisplayName= written by the importer: "EDHREC Average — <commander>"
# (the premade pull never passes bracket/budget, so no suffixes). The
# commander name is everything after the em-dash.
_EDHREC_DISPLAY_RE = re.compile(
    r"^DisplayName=EDHREC Average — (?P<name>.+)$", re.MULTILINE,
)

# Filename fallback: "[PREMADE] EDHREC <commander> [B<n>]" (stem). Less
# faithful than DisplayName (safe_filename strips non-ASCII), so it's
# only used when the metadata line is missing.
_EDHREC_STEM_RE = re.compile(
    r"^\[PREMADE\] EDHREC (?P<name>.+?)(?: \[B[1-5?]\])?$",
)

_SOURCE_EDHREC_RE = re.compile(r"^Source=edhrec$", re.MULTILINE)

_BASIC_NAMES = frozenset(
    {"forest", "island", "plains", "mountain", "swamp", "wastes"},
)


def _edhrec_premade_commander(text: str, stem: str) -> Optional[str]:
    """Recover the commander name of a broken EDHREC premade file."""
    m = _EDHREC_DISPLAY_RE.search(text)
    if m:
        return m.group("name").strip() or None
    m = _EDHREC_STEM_RE.match(stem)
    if m:
        return m.group("name").strip() or None
    return None


def repair_premade_text(text: str, commander_names: list[str]) -> str:
    """Rebuild a commander-less premade ``.dck`` text with a proper
    ``[Commander]`` section.

    - Inserts ``[Commander]`` (one ``1 <name>`` line per commander)
      directly above ``[Main]``.
    - Drops any [Main] line that IS a commander (full-name or front-face
      match) — the commander must occupy the command zone, not a slot.
    - Rebalances the mainboard onto the legal target
      (``100 - len(commander_names)``, the ``dck_utils.main_target``
      invariant) by adjusting the largest basic-land line(s); a deck
      with no basics to adjust is left as-is rather than guessed at.

    Pure text transform (no I/O) so tests can drive it directly.
    """
    keys: set[str] = set()
    for n in commander_names:
        lc = n.strip().lower()
        keys.add(lc)
        keys.add(lc.split("//")[0].strip())

    def _is_commander_line(name: str) -> bool:
        lc = name.lower()
        return lc in keys or lc.split("//")[0].strip() in keys

    lines = text.splitlines()
    out: list[str] = []
    main_header_at: Optional[int] = None
    main_card_at: list[int] = []  # indices into ``out`` of [Main] card lines
    in_main = False
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_main = stripped.lower() == "[main]"
            if in_main and main_header_at is None:
                main_header_at = len(out)
            out.append(ln)
            continue
        if in_main:
            parsed = parse_card_line(stripped)
            if parsed is not None:
                if parsed[1] and _is_commander_line(parsed[1]):
                    continue  # commander leaves [Main] for the command zone
                main_card_at.append(len(out))
        out.append(ln)

    # Rebalance [Main] onto the legal size via the basic-land lines.
    target = COMMANDER_DECK_SIZE - max(1, len(commander_names))
    total = 0
    basics: list[int] = []  # indices into ``out``, basics only
    for idx in main_card_at:
        parsed = parse_card_line(out[idx].strip())
        if parsed is None:
            continue
        total += parsed[0]
        if parsed[1].lower() in _BASIC_NAMES:
            basics.append(idx)
    delta = target - total
    if delta != 0 and basics:
        # Largest basic first: absorbs shrinks without zeroing small
        # lines, and is the least-wrong place to grow.
        basics.sort(
            key=lambda i: parse_card_line(out[i].strip())[0], reverse=True,
        )
        for idx in basics:
            if delta == 0:
                break
            qty, _name = parse_card_line(out[idx].strip())
            new_qty = max(0, qty + delta)
            delta -= new_qty - qty
            m = re.match(r"^(\d+)\s+(.*)$", out[idx].strip())
            out[idx] = f"{new_qty} {m.group(2)}" if new_qty > 0 else None
        out = [ln for ln in out if ln is not None]
        # Re-locate the [Main] header if line removal shifted it.
        main_header_at = next(
            (i for i, ln in enumerate(out)
             if ln.strip().lower() == "[main]"), main_header_at,
        )

    block = ["[Commander]"] + [f"1 {n}" for n in commander_names]
    insert_at = main_header_at if main_header_at is not None else len(out)
    out[insert_at:insert_at] = block
    return "\n".join(out) + "\n"


def repair_premades(out_dir: Path = DECK_OUT_DIR) -> int:
    """Fix on-disk EDHREC ``[PREMADE]`` decks missing their ``[Commander]``.

    Scans ``out_dir`` for premade-role files with ``Source=edhrec`` and
    zero ``[Commander]`` cards, recovers the commander from the
    ``DisplayName=`` metadata (filename-stem fallback), and rewrites the
    file via ``repair_premade_text``. Idempotent — a repaired (or
    correctly written) file has commander cards and is skipped, so
    re-runs are no-ops. Returns the number of files that could NOT be
    repaired (0 = success), mirroring the failure-count convention of
    ``moxfield_import.main``.
    """
    repaired = 0
    failures = 0
    if not out_dir.is_dir():
        print(f"  ERROR: deck dir not found: {out_dir}")
        return 1
    for path in sorted(out_dir.glob("*.dck")):
        if _mox._deck_role(path) != "premade":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"  ERROR reading {path.name}: {exc}")
            failures += 1
            continue
        if not _SOURCE_EDHREC_RE.search(text):
            continue  # Moxfield premades never shipped broken.
        if count_commander_cards(text) > 0:
            continue  # already correct (or already repaired).
        name = _edhrec_premade_commander(text, path.stem)
        if not name:
            print(f"  ERROR: cannot recover commander for {path.name}")
            failures += 1
            continue
        fixed = repair_premade_text(text, _commander_card_names(name))
        path.write_text(fixed, encoding="utf-8")
        repaired += 1
        print(f"  Repaired {path.name} (+[Commander] {name})")
    print(f"Premade repair: {repaired} repaired, {failures} failed.")
    return failures
