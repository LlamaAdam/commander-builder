"""End-to-end integration test exercising the full Phase 2 stack on real data.

Inputs: the 6 B3 user decks already on disk (Hakbal, Sep EDHREC, Celestial
Tribunal, Mothy, BlackPanther, Hash), plus the existing smoke-test
ComparisonReport JSON for Hakbal vs Hash.

Modules exercised:
  scryfall_client       — color identity lookup for every commander
  moxfield_push         — render each .dck as Moxfield textarea format
  compare_versions JSON — re-load the smoke result (no new Forge runs)
  analyst.analyze       — heuristic verdict on the real comparison
  knowledge_log         — record the iteration row + look it back up
  ml_dataset            — extract features + dataset summary

Pass criteria: the script runs without error, every section produces output,
and the final summary line prints `INTEGRATION TEST: PASS`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from commander_builder.analyst import (  # noqa: E402
    AnalystConfig,
    AnalystInput,
    analyze,
)
from commander_builder.forge_runner import VENDOR_FORGE  # noqa: E402
from commander_builder.knowledge_log import (  # noqa: E402
    Iteration,
    iterations_for_deck,
    record_iteration,
    stats_summary,
    update_verdict,
)
from commander_builder.ml_dataset import (  # noqa: E402
    build_dataset,
    dataset_summary,
    extract_features,
)
from commander_builder.moxfield_push import dck_to_textarea  # noqa: E402
from commander_builder.scryfall_client import color_identity_for_commander  # noqa: E402

DECK_DIR = VENDOR_FORGE / "userdata" / "decks" / "commander"
B3_DECKS = [
    "[USER] Hakbal of the Surging Soul [B3].dck",       # wfWOfj2sMU6FTaXco2D2wQ
    "[USER] Sep EDHREC Deck CHanges [B3].dck",          # Ujg1dQABs0qY3gx1mHPrew
    "[USER] Celestial Tribunal [B3].dck",               # c4pcG1q0v0Kv-V5W_aCZzA
    "[USER] Mothy [B3].dck",                            # LwISInoHxEGlPek_RdOpSg
    "[USER] BlackPanther [B3].dck",                     # CoyLbdO5k0W-zP68Eag4zw
    "[USER] Hash [B3].dck",                             # oot1ZNHbukKEGgrN9PdrRA
]


def _print_section(title: str) -> None:
    print()
    print("=" * 70)
    print(f" {title}")
    print("=" * 70)


def _safe_print(s: str) -> None:
    """Windows cp1252 chokes on emoji / unicode in opponent names. Reroute via
    stdout.buffer if needed."""
    try:
        print(s)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((s + "\n").encode("utf-8", errors="replace"))


# --- 1. Scryfall color identity for every deck -----------------------------

def test_scryfall() -> dict[str, str]:
    _print_section("1. Scryfall color identity (real network calls; cached on disk)")
    results: dict[str, str] = {}
    for deck in B3_DECKS:
        path = DECK_DIR / deck
        try:
            ci = color_identity_for_commander(path)
        except Exception as exc:
            ci = f"ERROR: {type(exc).__name__}: {exc}"
        results[deck] = ci
        _safe_print(f"  {deck:60s} -> {ci or '(colorless)'}")
    return results


# --- 2. Moxfield push format for every deck --------------------------------

def test_push_format() -> dict[str, int]:
    _print_section("2. Moxfield textarea-push format (line counts)")
    results: dict[str, int] = {}
    for deck in B3_DECKS:
        path = DECK_DIR / deck
        try:
            text = dck_to_textarea(path)
            lines = len(text.splitlines())
        except Exception as exc:
            print(f"  {deck}: ERROR {exc}")
            lines = -1
        results[deck] = lines
        # Forge commander decks have 100 cards (1 commander + 99 main) so we
        # expect ~100 lines when each card is on its own line. Partner pairs
        # produce 101.
        _safe_print(f"  {deck:60s} -> {lines} lines")
    return results


# --- 3. Load the existing smoke-test ComparisonReport ----------------------

def load_smoke_report() -> dict:
    _print_section("3. Load smoke-test ComparisonReport (Hakbal vs Hash, 20 games)")
    compare_dir = DECK_DIR / "_compare"
    if not compare_dir.exists():
        print(f"  SKIP: {compare_dir} not found.")
        return {}
    candidates = sorted(compare_dir.glob("USER_Hakbal*Hash*.json"))
    if not candidates:
        print(f"  SKIP: no Hakbal-vs-Hash comparison JSON in {compare_dir}.")
        return {}
    latest = candidates[-1]
    print(f"  Reading {latest.name}")
    data = json.loads(latest.read_text(encoding="utf-8"))
    print(f"  total_games={data['total_games']}, draws={data['draws']}, "
          f"winner={data['winner']}, margin={data['margin']}")
    print(f"  old_stats.wins={data['old_stats']['wins']}, "
          f"new_stats.wins={data['new_stats']['wins']}")
    print(f"  old_stats.avg_ending_life={data['old_stats']['avg_ending_life']}, "
          f"new_stats.avg_ending_life={data['new_stats']['avg_ending_life']}")
    print(f"  card_diff: +{len(data['card_diff'].get('added', []))} added, "
          f"-{len(data['card_diff'].get('removed', []))} removed")
    return data


# --- 4. Analyst verdict on the real comparison -----------------------------

def test_analyst(sim_report: dict) -> dict:
    _print_section("4. Analyst verdict on the real Hakbal-vs-Hash comparison")
    if not sim_report:
        print("  SKIP: no sim report available.")
        return {}
    manifest = {
        "added": sim_report.get("card_diff", {}).get("added", []),
        "removed": sim_report.get("card_diff", {}).get("removed", []),
        "rationale": "Synthetic test using Hakbal=v1 / Hash=v2 (different decks, not a real audit).",
        "audit_version": "test",
    }
    verdict = analyze(
        AnalystInput(
            deck_name="[USER] Hakbal of the Surging Soul [B3].dck",
            bracket=3,
            audit_manifest=manifest,
            sim_report=sim_report,
        ),
        config=AnalystConfig(),
    )
    print(f"  Verdict: {verdict.label} (confidence {verdict.confidence:.2f}, source {verdict.source})")
    print(f"  Reasoning: {verdict.reasoning}")
    if verdict.lessons:
        print("  Lessons:")
        for lesson in verdict.lessons:
            print(f"    - {lesson}")
    return {"verdict": verdict, "manifest": manifest}


# --- 5. knowledge_log persistence ------------------------------------------

def test_knowledge_log(
    sim_report: dict,
    verdict_data: dict,
    tmp_db: Path,
) -> int | None:
    _print_section("5. knowledge_log: record + retrieve iteration in a temp DB")
    if not sim_report or not verdict_data:
        print("  SKIP: missing sim/verdict.")
        return None
    verdict = verdict_data["verdict"]
    manifest = verdict_data["manifest"]

    decisive = sim_report["total_games"] - sim_report["draws"]
    win_rate_old = sim_report["old_stats"]["wins"] / decisive if decisive else 0.0
    win_rate_new = sim_report["new_stats"]["wins"] / decisive if decisive else 0.0
    margin = sim_report["new_stats"]["wins"] - sim_report["old_stats"]["wins"]

    rec = Iteration(
        deck_id="integration-test-hakbal",
        deck_name="[USER] Hakbal of the Surging Soul [B3].dck",
        bracket=3,
        audit_version="test",
        audit_manifest=manifest,
        sim_report=sim_report,
        verdict=verdict.label,
        verdict_notes=verdict.reasoning,
        win_rate_old=round(win_rate_old, 3),
        win_rate_new=round(win_rate_new, 3),
        margin=margin,
    )
    rid = record_iteration(rec, db_path=tmp_db)
    print(f"  Inserted iteration id={rid}")

    update_verdict(rid, verdict.label, verdict.reasoning, db_path=tmp_db)

    history = iterations_for_deck("integration-test-hakbal", db_path=tmp_db)
    print(f"  iterations_for_deck returned {len(history)} row(s); "
          f"last verdict={history[-1].verdict}")

    s = stats_summary(db_path=tmp_db)
    print(f"  stats_summary: {s}")
    return rid


# --- 6. ml_dataset feature extraction --------------------------------------

def test_ml_dataset(tmp_db: Path) -> dict:
    _print_section("6. ml_dataset: extract features + summary")
    history = iterations_for_deck("integration-test-hakbal", db_path=tmp_db)
    rows = build_dataset(history)
    if not rows:
        print("  SKIP: no rows extracted.")
        return {}
    row = rows[0]
    print(f"  Extracted {len(rows)} row(s).")
    print(f"  Features (first 10):")
    for k in list(row.features)[:10]:
        print(f"    {k:30s} = {row.features[k]}")
    print(f"  Feature vector length: {len(row.feature_vector())}")
    print(f"  Dataset summary: {dataset_summary(rows)}")
    return {"rows": len(rows), "feature_vector_len": len(row.feature_vector())}


# --- main ------------------------------------------------------------------

def main() -> int:
    print(f"\n{'#' * 70}")
    print(f"# Integration test on the 6 B3 decks ({len(B3_DECKS)})")
    print(f"# DECK_DIR: {DECK_DIR}")
    print(f"{'#' * 70}")

    # All 6 decks must already be on disk.
    missing = [d for d in B3_DECKS if not (DECK_DIR / d).exists()]
    if missing:
        print(f"\nERROR: {len(missing)} deck(s) missing from disk. Re-run moxfield_import.")
        for d in missing:
            print(f"  {d}")
        return 1

    color_results = test_scryfall()
    push_results = test_push_format()
    sim_report = load_smoke_report()
    verdict_data = test_analyst(sim_report)

    tmp_db = REPO / "integration_test_knowledge_log.sqlite"
    if tmp_db.exists():
        tmp_db.unlink()  # Fresh DB per run.
    iter_id = test_knowledge_log(sim_report, verdict_data, tmp_db)
    ml_results = test_ml_dataset(tmp_db)

    # --- Aggregate pass/fail check ----------------------------------------
    _print_section("INTEGRATION TEST SUMMARY")
    checks = {
        "scryfall_returned_identity_for_all_decks": all(
            isinstance(v, str) and not v.startswith("ERROR") for v in color_results.values()
        ),
        "push_format_produced_lines_for_all_decks": all(
            v > 50 for v in push_results.values()
        ),
        "smoke_report_loaded": bool(sim_report),
        "analyst_returned_verdict": bool(verdict_data),
        "knowledge_log_inserted_row": iter_id is not None,
        "ml_dataset_extracted_features": ml_results.get("feature_vector_len", 0) > 0,
    }
    for name, ok in checks.items():
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}")

    overall = all(checks.values())
    print()
    print(f"INTEGRATION TEST: {'PASS' if overall else 'FAIL'}")
    print(f"  knowledge_log DB: {tmp_db}")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
