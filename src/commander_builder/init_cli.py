"""``commander-init`` — guided first-run setup (decision B2).

WHY THIS EXISTS
===============
Everything a cold install needs already exists as a working command:
``commander-builder-bootstrap`` fetches Forge + a JRE, ``commander-oracle-
refresh --from-bulk`` primes the card store, ``commander-import --harvest``
pulls opponent candidates, ``commander-curate`` ranks them into a pool.
What did NOT exist was the ORDER. The steps are scattered across README's
Setup section, ``setup/forge/README.md``, a code block halfway down the
project-layout section, and the pool-curation data-flow diagram in
docs/architecture.md — and they are order-dependent (curation needs decks
on disk, decks want a warm card cache, everything needs the JVM). A new
operator reconstructs that sequence by reading four documents, which is
the "hours of unsequenced steps" complaint this command answers.

WHAT IT DOES *NOT* DO
=====================
It owns no new logic and no new state. Every step is a call into the
existing entry point (``bootstrap.download_forge``, ``oracle_store.main``,
``moxfield_import.main``, ``pool_curator.main``) — so behavior can't drift
from the standalone commands. Resumability likewise introduces **no
state file**: each step probes the artifact it produces (jar on disk,
snapshot count, candidate .dck files at the bracket, ``_pools/B<n>.json``)
and skips itself when that artifact is already there. Re-running is always
safe and is the intended way to continue after a failure or a decline.

Nothing expensive or network-touching happens without an explicit yes.
``--yes`` answers every prompt for unattended runs; ``--dry-run`` prints
the plan (with the live probe results) and calls nothing.

Public API
----------
``probe_dependencies`` / ``probe_oracle`` / ``probe_decks`` / ``probe_pool``
— the state checks, individually testable.
``run_init(opts)`` — the sequenced pipeline.
``main(argv)`` — the ``commander-init`` entry point.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # import-cost-free annotation only (see probe_dependencies)
    from .bootstrap import DependencyStatus

DEFAULT_BRACKET = 3

#: A snapshot store with at least this many cards counts as "primed".
#: The bulk path writes ~35k files (docs/CHANGELOG 2026-07-29); a cold
#: store that a few ad-hoc ``lookup_card`` calls have touched holds a
#: handful. Anything in between is ambiguous, so the threshold sits well
#: clear of incidental priming and well below a real bulk run.
ORACLE_PRIMED_MIN = 500

#: Enough bracket candidates that ``pool_curator`` won't bail (it needs
#: >= 4) with room for its preflight to reject a few. Below this the
#: acquisition step still has work to do.
DECKS_ENOUGH = 8

DECK_MODES = ("harvest", "premade", "skip")


@dataclass
class InitOptions:
    """Flags for one ``commander-init`` run."""
    bracket: int = DEFAULT_BRACKET
    assume_yes: bool = False
    dry_run: bool = False
    #: None → ask interactively which acquisition path to take.
    decks: Optional[str] = None


@dataclass
class StepResult:
    """Outcome of one step. ``status`` is one of:

    ``already``  — the probe found the work already done (resumed past it)
    ``done``     — we ran it and it succeeded
    ``skipped``  — operator declined, or a prerequisite is missing
    ``planned``  — ``--dry-run``: it would have run
    ``failed``   — the underlying command returned non-zero / raised
    """
    key: str
    status: str
    detail: str = ""
    rc: int = 0


# --------------------------------------------------------------------------- #
# Prompting
# --------------------------------------------------------------------------- #
def _read_line(prompt: str) -> str:
    """Read one answer from the operator.

    A named seam rather than a bare ``input()`` call so tests can drive
    the interactive path without a tty (and so a future web/desktop
    front-end can supply answers from somewhere else).
    """
    return input(prompt)


def _confirm(question: str, opts: InitOptions, *, default: bool = True) -> bool:
    """Ask before anything expensive. ``--yes`` answers yes without asking.

    ``default`` is what a bare Enter means. Steps that cost money, hours,
    or a large download pass ``default=False`` so the low-attention answer
    is the cheap one.
    """
    if opts.assume_yes:
        print(f"  {question} [--yes] yes")
        return True
    suffix = "[Y/n]" if default else "[y/N]"
    answer = _read_line(f"  {question} {suffix} ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def _announce(step_no: int, title: str, est: str, lines: list[str]) -> None:
    """Print the "here's what's about to happen, and for how long" block
    that every step opens with — the piece the scattered docs never had."""
    print("")
    print(f"[{step_no}/4] {title}")
    print(f"      time: {est}")
    for line in lines:
        print(f"      {line}")


# --------------------------------------------------------------------------- #
# Probes — every one reads an artifact that already exists, never a
# state file this command would have to keep honest.
# --------------------------------------------------------------------------- #
def probe_dependencies() -> "DependencyStatus":
    """Forge jar / JRE / mtg_cards presence (``bootstrap.check_dependencies``)."""
    from . import bootstrap
    return bootstrap.check_dependencies()


def probe_oracle() -> tuple[int, bool]:
    """(snapshot count, primed?) for the oracle snapshot store.

    Counts ``scryfall_client.CACHE_DIR/*.json`` and stops at the
    threshold — a warm store holds ~35k files and a full count on every
    run would be pointless I/O. ``CACHE_DIR`` is read off the module at
    CALL time (never frozen into an import-time constant) so the
    operator's ``MTG_CARDS_DIR`` and the tests' monkeypatches both land.
    """
    from . import scryfall_client
    cache_dir = Path(scryfall_client.CACHE_DIR)
    if not cache_dir.is_dir():
        return 0, False
    count = 0
    for _ in cache_dir.glob("*.json"):
        count += 1
        if count >= ORACLE_PRIMED_MIN:
            break
    return count, count >= ORACLE_PRIMED_MIN


def probe_decks(bracket: int) -> tuple[int, bool]:
    """(candidate count, enough?) for pool curation at ``bracket``.

    Delegates to ``pool_curator``'s own candidacy rule rather than
    re-deriving "which .dck files count" here: that rule is subtle
    ([USER]/[CONTROL]/[PREMADE] excluded, [REF] kept — see its docstring)
    and a second copy would silently drift from the curator that actually
    consumes the answer.
    """
    from . import pool_curator
    deck_dir = Path(pool_curator.DECK_DIR)
    if not deck_dir.is_dir():
        return 0, False
    candidates = pool_curator._list_bracket_candidates(bracket, deck_dir=deck_dir)
    return len(candidates), len(candidates) >= DECKS_ENOUGH


def probe_pool(bracket: int) -> tuple[Path, bool]:
    """(pool json path, exists?) — the artifact ``curate_bracket`` writes."""
    from . import pool_curator
    path = Path(pool_curator.POOL_DIR) / f"B{bracket}.json"
    return path, path.exists()


# --------------------------------------------------------------------------- #
# Step 1 — dependencies
# --------------------------------------------------------------------------- #
def step_dependencies(opts: InitOptions) -> StepResult:
    status = probe_dependencies()
    _announce(
        1, "External dependencies (Forge jar, Java, card cache)",
        "download ~120 MB Forge jar + ~40 MB JRE; minutes on a home connection",
        [
            f"Forge jar : {status.forge_jar or 'MISSING'}",
            f"Java/JRE  : {status.jre or 'MISSING'}",
            f"mtg_cards : {status.cards_dir or 'absent (step 2 fills it)'}",
        ],
    )

    # The card cache is step 2's job — a missing mtg_cards dir here is
    # not a reason to call this step incomplete.
    if status.forge_present and status.jre_present:
        print("      → already satisfied; nothing to download.")
        return StepResult("dependencies", "already",
                          "forge jar + java already on disk")

    wanted = [w for w in ("forge", "jre") if w in status.missing]
    print(f"      → would download: {', '.join(wanted)}")
    if opts.dry_run:
        return StepResult("dependencies", "planned", f"download {','.join(wanted)}")

    from . import bootstrap
    rc = 0
    detail = []
    if not status.forge_present:
        if _confirm("Download the latest Forge desktop jar (~120 MB)?", opts):
            try:
                jar = bootstrap.download_forge()
            except Exception as exc:  # noqa: BLE001 — operator-facing CLI:
                # one clear line beats a traceback for the "GitHub is down /
                # no network" class of failure. Re-run resumes here.
                print(f"      ! Forge download failed: {type(exc).__name__}: {exc}")
                rc = 1
                detail.append("forge failed")
            else:
                print(f"      ✓ Forge jar: {jar}")
                detail.append("forge downloaded")
        else:
            print("      - skipped. Sim steps stay unavailable until Forge exists.")
            detail.append("forge declined")

    if not status.jre_present:
        if _confirm("Download + extract a Temurin JRE 17 (~40 MB)?", opts):
            try:
                jre_dir = bootstrap.ensure_jre()
            except Exception as exc:  # noqa: BLE001 — see above.
                print(f"      ! JRE install failed: {type(exc).__name__}: {exc}")
                rc = 1
                detail.append("jre failed")
            else:
                print(f"      ✓ JRE: {jre_dir}")
                detail.append("jre installed")
        else:
            print("      - skipped. Forge needs Java 17+ to run at all.")
            detail.append("jre declined")

    if rc:
        return StepResult("dependencies", "failed", "; ".join(detail), rc=rc)
    if not detail or all(d.endswith("declined") for d in detail):
        return StepResult("dependencies", "skipped", "; ".join(detail))
    return StepResult("dependencies", "done", "; ".join(detail))


# --------------------------------------------------------------------------- #
# Step 2 — oracle bulk priming
# --------------------------------------------------------------------------- #
def step_oracle(opts: InitOptions) -> StepResult:
    count, primed = probe_oracle()
    _announce(
        2, "Prime the oracle card store from Scryfall's bulk export",
        "one ~150 MB rate-limit-exempt GET, then ~35k snapshot files written",
        [
            f"snapshots on disk: {'>=' if primed else ''}{count}",
            "runs: commander-oracle-refresh --from-bulk --everything",
        ],
    )
    if primed:
        print("      → already primed; nothing to fetch.")
        return StepResult("oracle", "already", f"{count}+ snapshots present")

    if opts.dry_run:
        print("      → would download the bulk file and write snapshots.")
        return StepResult("oracle", "planned", "bulk prime")

    # --everything, not --all: under --from-bulk, `--all` means "every card
    # named in the DECK DIR" (oracle_store._main_from_bulk), and at this
    # point in the sequence there are no decks yet — step 3 is what puts
    # them there. --everything is the cold-store prime the bulk path was
    # built for (11,721 missing snapshots, 2026-07 corpus run).
    if not _confirm(
        "Download the ~150 MB bulk file and write the snapshot store?",
        opts, default=False,
    ):
        # R3 F-09 (2026-09-03): the old line promised an on-demand prime
        # that only the NETWORKED callers do; `commander adopt` and the
        # judge are cache-only by design and never prime anything.
        print("      - skipped. Networked commands (dashboard, advise) prime "
              "per-card on demand instead (slower, 429-prone); the "
              "cache-only ones (adopt, judge) treat every un-primed card "
              "as unknown until you run: commander-oracle-refresh "
              "--from-bulk --everything")
        return StepResult("oracle", "skipped", "declined")

    from . import oracle_store
    rc = oracle_store.main(["--from-bulk", "--everything"])
    if rc:
        return StepResult("oracle", "failed", f"oracle-refresh exit {rc}", rc=rc)
    return StepResult("oracle", "done", "bulk snapshots written")


# --------------------------------------------------------------------------- #
# Step 3 — deck acquisition
# --------------------------------------------------------------------------- #
def _choose_deck_mode(opts: InitOptions) -> str:
    """Which acquisition path to take. ``--decks`` wins; ``--yes`` without
    it picks ``harvest`` because that is the one path that produces the
    pool candidates step 4 needs."""
    if opts.decks:
        return opts.decks
    if opts.assume_yes:
        print("  deck source [--yes] harvest")
        return "harvest"
    print("      1) harvest  — bulk-pull ~60 community decks at this bracket")
    print("      2) premade  — pull 10 Moxfield top-liked + 10 EDHREC average decks")
    print("      3) skip     — I'll paste/import my own decks later")
    answer = _read_line("  Choose 1/2/3 [1]: ").strip().lower()
    return {
        "": "harvest", "1": "harvest", "harvest": "harvest",
        "2": "premade", "premade": "premade",
        "3": "skip", "skip": "skip", "s": "skip",
    }.get(answer, "harvest")


def step_decks(opts: InitOptions) -> StepResult:
    count, enough = probe_decks(opts.bracket)
    _announce(
        3, f"Acquire decks for bracket B{opts.bracket}",
        "harvest: ~60 decks, one Moxfield API call each — several minutes, "
        "network-bound",
        [
            f"B{opts.bracket} pool candidates already on disk: {count} "
            f"(need >= {DECKS_ENOUGH} for a healthy curation)",
            "Moxfield's API is undocumented and ToS-gray (docs/architecture.md "
            "risk tiers) — harvest has no fallback source if it breaks.",
        ],
    )
    if enough:
        print("      → enough candidates already; nothing to pull.")
        return StepResult("decks", "already", f"{count} candidates on disk")

    if opts.dry_run:
        mode = opts.decks or "harvest (default under --yes; asked interactively)"
        print(f"      → would acquire decks via: {mode}")
        return StepResult("decks", "planned", f"acquire via {mode}")

    mode = _choose_deck_mode(opts)
    if mode == "skip":
        print("      - skipped. Import your own with `commander-import --user "
              "<moxfield-url>` (or the web app's paste box) before curating.")
        return StepResult("decks", "skipped", "operator will import manually")

    if mode == "premade":
        question = "Pull 10 Moxfield top-liked + 10 EDHREC average decks?"
        argv = ["--premade"]
    else:
        question = (f"Harvest ~60 community decks at B{opts.bracket} from "
                    "Moxfield?")
        argv = ["--harvest", str(opts.bracket)]

    if not _confirm(question, opts, default=False):
        print("      - skipped.")
        return StepResult("decks", "skipped", "declined")

    from . import moxfield_import
    rc = moxfield_import.main(argv)
    if rc:
        return StepResult("decks", "failed", f"commander-import exit {rc}", rc=rc)
    return StepResult("decks", "done", f"{mode}")


# --------------------------------------------------------------------------- #
# Step 4 — pool curation (the expensive one)
# --------------------------------------------------------------------------- #
#: Printed BEFORE the curation prompt, never after. This is the only step
#: that spends hours of JVM time, and an operator who learns that after
#: saying yes has been ambushed. Numbers are the measured wall-times from
#: docs/architecture.md ("Data flow — pool curation"), not estimates.
JVM_COST_WARNING = (
    "COST WARNING: curation runs Forge on the JVM — measured wall-time "
    "~35 min for B3 and ~55 min for B5 (cEDH games are slower). It pins a "
    "CPU core for that whole time. One-time per bracket: the pool is reused "
    "until it is 30 days old or you pass --recurate."
)


def step_pool(opts: InitOptions) -> StepResult:
    pool_path, exists = probe_pool(opts.bracket)
    _announce(
        4, f"Curate the canonical B{opts.bracket} opponent pool",
        "~35 min (B3) / ~55 min (B5) of Forge simulation",
        [
            f"pool artifact: {pool_path} "
            f"({'present' if exists else 'missing'})",
            "runs: commander-curate --bracket "
            f"{opts.bracket} --max-candidates 12",
        ],
    )
    if exists:
        print("      → pool already curated; nothing to run.")
        return StepResult("pool", "already", f"{pool_path} exists")

    status = probe_dependencies()
    deps_ok = status.forge_present and status.jre_present
    count, enough = probe_decks(opts.bracket)

    # Under --dry-run the earlier steps haven't run, so an unmet
    # prerequisite is a note about the plan's ORDER, not a skip: the
    # operator is being shown what a full run would do. The cost warning
    # prints either way — the whole point of the dry run is to see the
    # bill before agreeing to it.
    if opts.dry_run:
        if not deps_ok:
            missing = ", ".join(m for m in status.missing if m in ("forge", "jre"))
            print(f"      note: needs {missing} from step 1 first.")
        if count < 4:
            print(f"      note: needs >= 4 B{opts.bracket} candidates from "
                  f"step 3 first (have {count}).")
        print(f"      {JVM_COST_WARNING}")
        print("      → would run the curation once the steps above are done.")
        return StepResult("pool", "planned", "curate")

    # A curation with no Forge/Java is a guaranteed failure after a long
    # startup — refuse it here with the reason instead.
    if not deps_ok:
        missing = ", ".join(m for m in status.missing if m in ("forge", "jre"))
        print(f"      - skipped: {missing} missing (step 1). Re-run "
              "commander-init after installing it.")
        return StepResult("pool", "skipped", f"{missing} missing")

    if count < 4:
        print(f"      - skipped: only {count} B{opts.bracket} candidates on "
              "disk, the curator needs >= 4 (step 3).")
        return StepResult("pool", "skipped", f"only {count} candidates")
    if not enough:
        print(f"      note: {count} candidates is thin (>= {DECKS_ENOUGH} "
              "recommended); the pool will be less discriminating.")

    print(f"      {JVM_COST_WARNING}")
    if not _confirm(f"Run the B{opts.bracket} curation now?", opts, default=False):
        print("      - skipped. Run `commander-curate --bracket "
              f"{opts.bracket}` when you have the time budget.")
        return StepResult("pool", "skipped", "declined")

    from . import pool_curator
    rc = pool_curator.main(["--bracket", str(opts.bracket)])
    if rc:
        return StepResult("pool", "failed", f"commander-curate exit {rc}", rc=rc)
    return StepResult("pool", "done", str(pool_path))


STEPS = (step_dependencies, step_oracle, step_decks, step_pool)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run_init(opts: InitOptions) -> int:
    """Walk the four steps in order. Returns 0 unless a step failed.

    A failed step does NOT abort the rest: the later steps have their own
    prerequisite checks and will skip themselves with a reason, which
    gives the operator the whole picture in one pass instead of one
    problem per run.
    """
    print("commander-init — guided first-run setup"
          + (" (dry run: nothing will be downloaded or run)" if opts.dry_run
             else ""))
    print(f"target bracket: B{opts.bracket}")

    results: list[StepResult] = []
    for step in STEPS:
        results.append(step(opts))

    print("")
    print("Summary")
    for res in results:
        detail = f" — {res.detail}" if res.detail else ""
        print(f"  {res.key:13s} {res.status}{detail}")

    failed = [r for r in results if r.status == "failed"]
    if failed:
        print("")
        print(f"{len(failed)} step(s) failed. Fix the cause and re-run "
              "`commander-init` — completed steps are detected and skipped.")
        return 1

    remaining = [r for r in results if r.status == "skipped"]
    if remaining and not opts.dry_run:
        print("")
        print("Some steps were skipped. Re-run `commander-init` any time; "
              "it resumes where you left off.")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    """``commander-init`` — sequence the real first-run pipeline."""
    import argparse

    p = argparse.ArgumentParser(
        prog="commander-init",
        description=(
            "Guided first-run setup: external dependencies → oracle card "
            "store → decks → curated opponent pool, in the order they "
            "actually depend on each other. Every step checks whether it is "
            "already done and skips itself, so re-running resumes."
        ),
    )
    p.add_argument("--bracket", type=int, default=DEFAULT_BRACKET,
                   help=f"Commander bracket to set up for (default "
                        f"{DEFAULT_BRACKET}). Decides which decks are "
                        "harvested and which pool is curated.")
    p.add_argument("--yes", "-y", dest="assume_yes", action="store_true",
                   help="Answer yes to every prompt (unattended runs). "
                        "This DOES authorize the ~150 MB download and the "
                        "multi-hour curation.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the plan and the current state of each step, "
                        "then exit. Downloads nothing, runs nothing.")
    p.add_argument("--decks", choices=DECK_MODES, default=None,
                   help="Deck-acquisition path: harvest (~60 community decks "
                        "at the bracket), premade (10 Moxfield top-liked + 10 "
                        "EDHREC average), or skip. Default: ask (harvest "
                        "under --yes).")
    args = p.parse_args(argv)

    if not 1 <= args.bracket <= 5:
        print(f"ERROR: bracket {args.bracket} out of range (1-5).", flush=True)
        return 2

    return run_init(InitOptions(
        bracket=args.bracket,
        assume_yes=args.assume_yes,
        dry_run=args.dry_run,
        decks=args.decks,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
