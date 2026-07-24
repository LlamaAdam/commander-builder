"""Merge soak JSONL outputs from one or more machines — separate *and* together.

Each machine writes its own ``soak_throughput.jsonl`` (so the sets are
inherently tracked separately, per file). This tool combines them into one
dataset while **preserving provenance**: every row is tagged with a
``source`` label, and the run prints a per-source breakdown plus the
grand total — so you see machine-1 vs machine-2 counts AND the sum.

Label each input explicitly with ``LABEL=PATH`` (recommended), or pass a
bare path (source = the file's stem). Rows are independent samples, so
combining is concatenation; byte-identical lines are de-duplicated
(guards against passing the same file twice).

Usage:
  python scripts/merge_soak.py box1=C:/Users/pilot/soak_throughput.jsonl box2=D:/in/box2.jsonl
  python scripts/merge_soak.py *.jsonl --to-knowledge-log
"""
from __future__ import annotations

import argparse
import glob
import json
from collections import defaultdict
from pathlib import Path


def _parse_input(arg: str) -> tuple[str | None, str]:
    """``LABEL=PATH`` -> (LABEL, PATH); bare ``PATH`` -> (None, PATH).

    Splits on the FIRST ``=`` only. Windows drive paths use ``:`` (e.g.
    ``C:/Users/...``), not ``=``, so this never mis-splits a path."""
    if "=" in arg:
        label, _, pat = arg.partition("=")
        if label and pat:
            return label, pat
    return None, arg


def load_tagged(inputs: list[str]) -> list[dict]:
    """Load rows from all inputs, tagging each with a ``source`` label
    (explicit LABEL > file stem). Keeps any embedded ``host`` the soak
    wrote. De-dups byte-identical lines across all inputs."""
    seen: set[str] = set()
    rows: list[dict] = []
    for arg in inputs:
        label, pat = _parse_input(arg)
        matched = glob.glob(pat)
        if not matched:
            print(f"WARNING: no files matched {pat!r}")
        for path in matched:
            src = label or Path(path).stem
            try:
                text = Path(path).read_text(encoding="utf-8")
            except OSError as exc:
                print(f"WARNING: cannot read {path}: {exc}")
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line or line in seen:
                    continue
                seen.add(line)
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                r.setdefault("host", None)   # soak_pool may embed this
                r["source"] = src            # explicit per-file provenance
                rows.append(r)
    return rows


def report(rows: list[dict]) -> dict:
    """Print per-source rows/done/games + the combined TOTAL."""
    agg: dict[str, dict] = defaultdict(lambda: {"rows": 0, "done": 0, "games": 0})
    for r in rows:
        a = agg[r["source"]]
        a["rows"] += 1
        # loop_unattributed = an honest short row (batch cut by an
        # unattributable looping game); its games all completed, so it
        # counts as data alongside 'done' rows.
        if r.get("status") in ("done", "loop_unattributed"):
            a["done"] += 1
            a["games"] += r.get("games") or 0
    print(f"{'source':<28}{'rows':>7}{'done':>7}{'games':>9}")
    print("-" * 51)
    tot = {"rows": 0, "done": 0, "games": 0}
    for src in sorted(agg):
        a = agg[src]
        print(f"{src:<28}{a['rows']:>7}{a['done']:>7}{a['games']:>9}")
        for k in tot:
            tot[k] += a[k]
    print("-" * 51)
    print(f"{'TOTAL':<28}{tot['rows']:>7}{tot['done']:>7}{tot['games']:>9}")
    return tot


def _is_gauntlet_row(r: dict) -> bool:
    """True for rows written by ``soak_pool._record_gauntlet``.

    Gauntlet-mode soak rows are a SEPARATE experiment with a SEPARATE
    schema (one test deck vs a FIXED 3-deck field): they carry
    ``mode="gauntlet"``, ``test_deck``/``role``/``pair_base``, and
    per-deck ``wins``/``losses``/``draws`` — and NO ``deck_a``/``deck_b``
    or ``wins_a``/``wins_b``. They still have ``status='done'``, so
    before this check the AB fold below happily "folded" them: every
    field lookup missed, producing a bogus ``deck_id='?'`` 0-0 neutral
    iteration per gauntlet row. Those rows must be skipped, never
    shoehorned into the AB iteration shape.

    The explicit ``mode`` tag is the primary signal; the shape check is
    belt-and-suspenders for a hand-edited row that lost the tag (per-deck
    ``wins``/``losses`` present while the AB pair field is absent can
    only be the gauntlet shape).
    """
    if r.get("mode") == "gauntlet":
        return True
    return ("wins" in r or "losses" in r) and "deck_b" not in r


def _soak_identity(*, deck_a, deck_b, ts, host, games, wins_a, wins_b) -> str:
    """Content-identity hash for one folded soak row.

    Why not export.py's natural key (deck_id, created_at, deck_name)?
    Soak JSONL rows have no ``created_at`` — knowledge_log stamps that at
    record time, so it differs on every fold and would defeat dedupe.
    Mutable DB columns (``verdict`` can be PATCHed later; the verdict/
    margin policy is a --margin flag) must not participate either, or a
    refold after an edit would re-insert. Identity is therefore the
    STABLE FACTS of the sim itself:

      * the two decks (deck_a/deck_b),
      * the score (games/wins_a/wins_b),
      * the provenance stamps soak_pool wrote into the row (``ts`` is
        stamped once at sim completion and carried verbatim through
        merge, so two sims of the same pair that happen to land the same
        score still hash differently; ``host`` separates machines).

    The merge-time ``source`` label is deliberately EXCLUDED: it names
    the file/label a row arrived through, not the sim — the same row
    re-merged under a different label (e.g. a combined output re-fed
    alongside its originals) is still the same sim and must dedupe.

    Hashing goes through ``knowledge_log.canonical_content_hash`` — the
    exact canonicalization export.py's import dedupe uses (promoted out
    of export.py so this fold doesn't copy-paste it).

    Invariant: the same JSONL row folded twice -> one DB row. Rows folded
    by the pre-fix code lack ``ts`` in their manifest, so they hash
    differently and are NOT retro-deduped — this is fix-forward only (the
    pre-fix rows are already potentially double-counted; nothing recorded
    then can distinguish a true duplicate from a coincidental rematch).
    """
    from commander_builder.knowledge_log import canonical_content_hash
    return canonical_content_hash({
        "deck_a": deck_a, "deck_b": deck_b, "ts": ts, "host": host,
        "games": games, "wins_a": wins_a, "wins_b": wins_b,
    })


def fold_to_knowledge_log(rows: list[dict], db_path, margin: int) -> dict:
    """Write completed AB sims to knowledge_log. ``source``/``host``/``ts``
    are carried into each row's audit_manifest so the machines stay
    distinguishable while all counting toward one dataset.

    Returns counters ``{"written", "skipped_duplicate", "skipped_gauntlet"}``.

    IDEMPOTENT (2026-07-19): this fold used to re-INSERT every 'done' row
    on every run — ``record_iteration`` is append-only and nothing
    deduped — so re-running the same merge silently double-counted the
    dataset the FP-002/FP-013 row gates read. Candidate rows are now
    hashed over their stable facts (see ``_soak_identity``) and skipped
    when an identical soak-ab row already exists.

    Gauntlet-mode rows are SKIPPED, not folded — separate schema, see
    ``_is_gauntlet_row``.
    """
    from commander_builder.knowledge_log import (
        Iteration, _resolve_db_path, all_iterations, decisive_win_rate,
        record_iteration,
    )
    from commander_builder.web._helpers import _bracket_from_filename

    # _resolve_db_path (not a copy of DEFAULT_DB_PATH) so the default DB
    # is looked up at call time — the test suite's autouse isolation
    # fixture monkeypatches knowledge_log.DEFAULT_DB_PATH.
    db = Path(db_path) if db_path else _resolve_db_path(None)

    # Pre-scan the destination ONCE and index existing soak-fold rows by
    # content identity. Only audit_version='soak-ab' rows are indexed:
    # they're the only rows this fold can collide with, and their
    # audit_manifest/sim_report carry exactly the fields _soak_identity
    # reads. Personal-project scale (thousands of rows, not millions)
    # makes one in-memory set far simpler than per-row SQL lookups, and
    # it lets rows inserted BY THIS fold participate in dedupe too.
    seen: set[str] = set()
    for it in all_iterations(db_path=db):
        if it.audit_version != "soak-ab":
            continue
        m = it.audit_manifest or {}
        s = it.sim_report or {}
        seen.add(_soak_identity(
            deck_a=m.get("deck_a"), deck_b=m.get("deck_b"),
            ts=m.get("ts"), host=m.get("host"),
            # `or 0` mirrors the write-side normalization below, so a
            # None-vs-0 representation difference can't defeat the match.
            games=s.get("games") or 0,
            wins_a=s.get("wins_a") or 0, wins_b=s.get("wins_b") or 0,
        ))

    written = 0
    skipped_duplicate = 0
    skipped_gauntlet = 0
    for r in rows:
        if r.get("status") != "done":
            continue
        if _is_gauntlet_row(r):
            skipped_gauntlet += 1
            continue
        wa = r.get("wins_a") or 0
        wb = r.get("wins_b") or 0
        g = r.get("games") or 0
        # Normalize deck_b BEFORE hashing: the identity must match what
        # the manifest stores (the DB-side pre-scan reads the manifest,
        # which holds the normalized value).
        deck_b = r.get("deck_b") or "?.dck"
        identity = _soak_identity(
            deck_a=r.get("deck_a"), deck_b=deck_b, ts=r.get("ts"),
            host=r.get("host"), games=g, wins_a=wa, wins_b=wb,
        )
        if identity in seen:
            skipped_duplicate += 1
            continue
        # Win-rate convention (2026-07-19, knowledge_log schema docstring):
        # wins / DECISIVE games. For AB-shaped soak rows the attributed-
        # winner count is wa + wb (games includes filler-won / unresolved-
        # draw games) — same denominator _ab_to_iteration_fields uses, so
        # soak-fold rows stay comparable with every other writer's rows.
        decisive = wa + wb
        delta = wb - wa
        verdict = ("kept" if delta >= margin
                   else "reverted" if delta <= -margin else "neutral")
        it = Iteration(
            deck_id=Path(deck_b).stem,
            deck_name=Path(deck_b).stem,
            bracket=_bracket_from_filename(deck_b) or 3,
            audit_version="soak-ab",
            audit_manifest={"deck_a": r.get("deck_a"), "deck_b": deck_b,
                            "source": r.get("source"), "host": r.get("host"),
                            # ts anchors content identity for refold
                            # dedupe (see _soak_identity) AND is useful
                            # provenance: when the sim actually ran, not
                            # just when it was folded.
                            "ts": r.get("ts"),
                            "games": g},
            sim_report={"wins_a": wa, "wins_b": wb, "games": g},
            verdict=verdict,
            win_rate_old=decisive_win_rate(wa, decisive),
            win_rate_new=decisive_win_rate(wb, decisive),
            margin=delta,
        )
        record_iteration(it, db_path=db)
        # Register the fresh row immediately so a duplicate later in this
        # same batch (same row arriving through two differently-labeled
        # input files — load_tagged's byte-dedupe misses those because
        # the `source` tag differs) also collapses to one insert.
        seen.add(identity)
        written += 1
    return {"written": written, "skipped_duplicate": skipped_duplicate,
            "skipped_gauntlet": skipped_gauntlet}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="merge_soak")
    ap.add_argument("inputs", nargs="+",
                    help="JSONL paths or LABEL=PATH (globs ok).")
    ap.add_argument("--out", type=Path, default=Path("combined_soak.jsonl"))
    ap.add_argument("--to-knowledge-log", action="store_true",
                    help="Also fold completed sims into knowledge_log.")
    ap.add_argument("--db-path", default=None)
    ap.add_argument("--margin", type=int, default=1)
    args = ap.parse_args(argv)

    rows = load_tagged(args.inputs)
    if not rows:
        raise SystemExit("no rows loaded")
    args.out.write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    print(f"combined -> {args.out}\n")
    report(rows)
    if args.to_knowledge_log:
        res = fold_to_knowledge_log(rows, args.db_path, args.margin)
        print(f"\nwrote {res['written']} iterations to knowledge_log "
              f"(audit_version='soak-ab'; source/host/ts in manifest)")
        # Always print the dedupe count (even 0) so a re-run visibly
        # reports itself as a no-op instead of looking like it did work.
        print(f"skipped {res['skipped_duplicate']} already-folded rows "
              f"(content-identity dedupe; re-running the same merge is a no-op)")
        if res["skipped_gauntlet"]:
            print(f"skipped {res['skipped_gauntlet']} gauntlet-mode rows: "
                  f"separate schema (test deck vs fixed gauntlet, wins/losses"
                  f") — not foldable into the AB iteration shape")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
