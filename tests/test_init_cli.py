"""Tests for ``commander-init``, the guided first-run setup (decision B2).

Everything here is offline: ``bootstrap`` (network), ``oracle_store``
(150MB GET), ``moxfield_import`` (Moxfield API) and ``pool_curator``
(the JVM) are all stubbed at the module boundary, which is exactly the
seam ``init_cli`` uses — it never reimplements their work, it calls them.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from commander_builder import init_cli


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeStatus:
    """Stand-in for ``bootstrap.DependencyStatus``."""

    def __init__(self, forge=True, jre=True, cards=True):
        self.forge_jar = Path("/forge/forge.jar") if forge else None
        self.jre = Path("/jre/bin/java") if jre else None
        self.cards_dir = Path("/mtg_cards") if cards else None
        self.notes = []

    forge_present = property(lambda self: self.forge_jar is not None)
    jre_present = property(lambda self: self.jre is not None)
    cards_present = property(lambda self: self.cards_dir is not None)

    @property
    def missing(self):
        out = []
        if not self.forge_present:
            out.append("forge")
        if not self.jre_present:
            out.append("jre")
        if not self.cards_present:
            out.append("mtg_cards")
        return out


class Calls:
    """Records every expensive call the steps could make."""

    def __init__(self):
        self.log: list[tuple] = []

    def record(self, name):
        def fn(*args, **kwargs):
            self.log.append((name, args, kwargs))
            return 0
        return fn

    def names(self):
        return [c[0] for c in self.log]


@pytest.fixture
def calls(monkeypatch):
    """Stub every network/JVM entry point ``init_cli`` can reach."""
    from commander_builder import bootstrap, moxfield_import, oracle_store
    from commander_builder import pool_curator

    rec = Calls()
    monkeypatch.setattr(bootstrap, "download_forge", rec.record("download_forge"))
    monkeypatch.setattr(bootstrap, "ensure_jre", rec.record("ensure_jre"))
    monkeypatch.setattr(oracle_store, "main", rec.record("oracle_main"))
    monkeypatch.setattr(moxfield_import, "main", rec.record("import_main"))
    monkeypatch.setattr(pool_curator, "main", rec.record("curate_main"))
    return rec


@pytest.fixture
def probes(monkeypatch):
    """All four probes report 'nothing done yet' by default; tests flip
    the ones they care about."""
    state = {
        "deps": FakeStatus(forge=True, jre=True, cards=False),
        "oracle": (0, False),
        "decks": (0, False),
        "pool": (Path("/pools/B3.json"), False),
    }
    monkeypatch.setattr(init_cli, "probe_dependencies", lambda: state["deps"])
    monkeypatch.setattr(init_cli, "probe_oracle", lambda: state["oracle"])
    monkeypatch.setattr(init_cli, "probe_decks", lambda b: state["decks"])
    monkeypatch.setattr(init_cli, "probe_pool", lambda b: state["pool"])
    return state


def _answers(monkeypatch, *replies):
    """Feed scripted answers to the interactive prompt seam."""
    queue = list(replies)

    def fake_read(prompt):
        assert queue, f"unexpected prompt: {prompt!r}"
        return queue.pop(0)

    monkeypatch.setattr(init_cli, "_read_line", fake_read)
    return queue


# --------------------------------------------------------------------------- #
# Probes (each true/false)
# --------------------------------------------------------------------------- #
def test_probe_oracle_counts_snapshots(tmp_path, monkeypatch):
    from commander_builder import scryfall_client
    cache = tmp_path / "snaps"
    cache.mkdir()
    monkeypatch.setattr(scryfall_client, "CACHE_DIR", cache)
    monkeypatch.setattr(init_cli, "ORACLE_PRIMED_MIN", 3)

    assert init_cli.probe_oracle() == (0, False)
    for i in range(2):
        (cache / f"c{i}.json").write_text("{}", encoding="utf-8")
    assert init_cli.probe_oracle() == (2, False)
    (cache / "c2.json").write_text("{}", encoding="utf-8")
    count, primed = init_cli.probe_oracle()
    assert primed and count >= 3


def test_probe_oracle_false_when_cache_dir_absent(tmp_path, monkeypatch):
    from commander_builder import scryfall_client
    monkeypatch.setattr(scryfall_client, "CACHE_DIR", tmp_path / "nope")
    assert init_cli.probe_oracle() == (0, False)


def test_probe_decks_uses_the_curators_candidacy_rule(tmp_path, monkeypatch):
    from commander_builder import pool_curator
    decks = tmp_path / "decks"
    decks.mkdir()
    monkeypatch.setattr(pool_curator, "DECK_DIR", decks)
    monkeypatch.setattr(init_cli, "DECKS_ENOUGH", 2)

    assert init_cli.probe_decks(3) == (0, False)
    for name in ("Alpha [B3].dck", "Beta [B3].dck"):
        (decks / name).write_text("", encoding="utf-8")
    # Excluded by pool_curator's candidacy rule: the user's own deck and
    # popularity-ranked premades are never pool candidates.
    for name in ("[USER] Mine [B3].dck", "[PREMADE] Pop [B3].dck"):
        (decks / name).write_text("", encoding="utf-8")
    # Another bracket's deck counts for that bracket only.
    (decks / "Gamma [B4].dck").write_text("", encoding="utf-8")

    assert init_cli.probe_decks(3) == (2, True)
    assert init_cli.probe_decks(4) == (1, False)
    assert init_cli.probe_decks(5) == (0, False)


def test_probe_pool_checks_the_bracket_json(tmp_path, monkeypatch):
    from commander_builder import pool_curator
    pools = tmp_path / "_pools"
    pools.mkdir()
    monkeypatch.setattr(pool_curator, "POOL_DIR", pools)

    path, exists = init_cli.probe_pool(3)
    assert path == pools / "B3.json" and not exists
    (pools / "B3.json").write_text("{}", encoding="utf-8")
    assert init_cli.probe_pool(3)[1] is True
    assert init_cli.probe_pool(5)[1] is False


def test_probe_dependencies_delegates_to_bootstrap(monkeypatch):
    from commander_builder import bootstrap
    sentinel = FakeStatus()
    monkeypatch.setattr(bootstrap, "check_dependencies", lambda: sentinel)
    assert init_cli.probe_dependencies() is sentinel


# --------------------------------------------------------------------------- #
# Step skipping — the resumability contract
# --------------------------------------------------------------------------- #
def test_dependencies_step_skips_when_already_installed(probes, calls, capsys):
    probes["deps"] = FakeStatus(forge=True, jre=True, cards=True)
    res = init_cli.step_dependencies(init_cli.InitOptions())
    assert res.status == "already"
    assert calls.names() == []
    assert "already satisfied" in capsys.readouterr().out


def test_dependencies_step_downloads_only_what_is_missing(probes, calls):
    probes["deps"] = FakeStatus(forge=False, jre=True, cards=True)
    res = init_cli.step_dependencies(init_cli.InitOptions(assume_yes=True))
    assert res.status == "done"
    assert calls.names() == ["download_forge"]


def test_dependencies_step_installs_jre_when_java_missing(probes, calls):
    probes["deps"] = FakeStatus(forge=True, jre=False, cards=True)
    init_cli.step_dependencies(init_cli.InitOptions(assume_yes=True))
    assert calls.names() == ["ensure_jre"]


def test_dependencies_step_reports_failure_without_raising(probes, monkeypatch,
                                                           capsys):
    from commander_builder import bootstrap
    probes["deps"] = FakeStatus(forge=False, jre=True)

    def boom():
        raise OSError("no network")

    monkeypatch.setattr(bootstrap, "download_forge", boom)
    res = init_cli.step_dependencies(init_cli.InitOptions(assume_yes=True))
    assert res.status == "failed" and res.rc == 1
    assert "no network" in capsys.readouterr().out


def test_oracle_step_skips_when_store_is_primed(probes, calls, capsys):
    probes["oracle"] = (35000, True)
    res = init_cli.step_oracle(init_cli.InitOptions())
    assert res.status == "already"
    assert calls.names() == []
    assert "already primed" in capsys.readouterr().out


def test_oracle_step_runs_the_bulk_path_when_cold(probes, calls):
    probes["oracle"] = (12, False)
    res = init_cli.step_oracle(init_cli.InitOptions(assume_yes=True))
    assert res.status == "done"
    assert calls.log == [("oracle_main", (["--from-bulk", "--everything"],), {})]


def test_oracle_step_propagates_failure(probes, monkeypatch):
    from commander_builder import oracle_store
    probes["oracle"] = (0, False)
    monkeypatch.setattr(oracle_store, "main", lambda argv: 2)
    res = init_cli.step_oracle(init_cli.InitOptions(assume_yes=True))
    assert res.status == "failed" and res.rc == 2


def test_decks_step_skips_when_candidates_present(probes, calls, capsys):
    probes["decks"] = (30, True)
    res = init_cli.step_decks(init_cli.InitOptions())
    assert res.status == "already"
    assert calls.names() == []
    assert "enough candidates" in capsys.readouterr().out


def test_decks_step_harvests_by_bracket(probes, calls):
    probes["decks"] = (0, False)
    res = init_cli.step_decks(
        init_cli.InitOptions(bracket=4, assume_yes=True, decks="harvest"))
    assert res.status == "done"
    assert calls.log == [("import_main", (["--harvest", "4"],), {})]


def test_decks_step_premade_path(probes, calls):
    res = init_cli.step_decks(
        init_cli.InitOptions(assume_yes=True, decks="premade"))
    assert res.status == "done"
    assert calls.log == [("import_main", (["--premade"],), {})]


def test_decks_step_skip_mode_calls_nothing(probes, calls, capsys):
    res = init_cli.step_decks(init_cli.InitOptions(assume_yes=True, decks="skip"))
    assert res.status == "skipped"
    assert calls.names() == []
    assert "commander-import --user" in capsys.readouterr().out


def test_decks_step_interactive_choice(probes, calls, monkeypatch):
    _answers(monkeypatch, "2", "y")  # premade, then confirm
    res = init_cli.step_decks(init_cli.InitOptions())
    assert res.status == "done"
    assert calls.log == [("import_main", (["--premade"],), {})]


def test_pool_step_skips_when_pool_exists(probes, calls, capsys):
    probes["pool"] = (Path("/pools/B3.json"), True)
    res = init_cli.step_pool(init_cli.InitOptions())
    assert res.status == "already"
    assert calls.names() == []
    assert "already curated" in capsys.readouterr().out


def test_pool_step_skips_without_forge_or_java(probes, calls, capsys):
    probes["deps"] = FakeStatus(forge=False, jre=True)
    probes["decks"] = (12, True)
    res = init_cli.step_pool(init_cli.InitOptions())
    assert res.status == "skipped" and "forge" in res.detail
    assert calls.names() == []


def test_pool_step_skips_when_too_few_candidates(probes, calls, capsys):
    probes["decks"] = (2, False)
    res = init_cli.step_pool(init_cli.InitOptions())
    assert res.status == "skipped"
    assert calls.names() == []
    assert ">= 4" in capsys.readouterr().out


def test_pool_step_runs_curation(probes, calls):
    probes["decks"] = (12, True)
    res = init_cli.step_pool(init_cli.InitOptions(bracket=5, assume_yes=True))
    assert res.status == "done"
    assert calls.log == [("curate_main", (["--bracket", "5"],), {})]


# --------------------------------------------------------------------------- #
# The JVM cost warning
# --------------------------------------------------------------------------- #
def test_jvm_cost_warning_precedes_the_curation_prompt(probes, calls,
                                                       monkeypatch, capsys):
    probes["decks"] = (12, True)
    prompts: list[str] = []

    def fake_read(prompt):
        # Capture what has been printed at the moment we're asked.
        prompts.append(capsys.readouterr().out)
        return "n"

    monkeypatch.setattr(init_cli, "_read_line", fake_read)
    init_cli.step_pool(init_cli.InitOptions())

    assert prompts, "curation must ask before spending JVM time"
    before_prompt = prompts[0]
    assert "COST WARNING" in before_prompt
    assert "~35 min" in before_prompt and "~55 min" in before_prompt
    assert calls.names() == []


def test_declining_curation_leaves_a_resume_hint(probes, calls, monkeypatch,
                                                 capsys):
    probes["decks"] = (12, True)
    _answers(monkeypatch, "n")
    res = init_cli.step_pool(init_cli.InitOptions())
    assert res.status == "skipped"
    assert "commander-curate --bracket 3" in capsys.readouterr().out
    assert calls.names() == []


# --------------------------------------------------------------------------- #
# --dry-run / --yes
# --------------------------------------------------------------------------- #
def test_dry_run_makes_no_calls_and_asks_nothing(probes, calls, monkeypatch,
                                                 capsys):
    probes["deps"] = FakeStatus(forge=False, jre=False, cards=False)

    def no_prompts(prompt):  # pragma: no cover — asserted not to run
        raise AssertionError("--dry-run must not prompt")

    monkeypatch.setattr(init_cli, "_read_line", no_prompts)
    rc = init_cli.run_init(init_cli.InitOptions(dry_run=True))
    assert rc == 0
    assert calls.names() == []
    out = capsys.readouterr().out
    assert "nothing will be downloaded or run" in out
    # The plan still shows the bill for the expensive step.
    assert "COST WARNING" in out
    assert out.count("planned") >= 4


def test_dry_run_still_reports_already_done_steps(probes, calls, capsys):
    probes["deps"] = FakeStatus(forge=True, jre=True, cards=True)
    probes["oracle"] = (35000, True)
    init_cli.run_init(init_cli.InitOptions(dry_run=True))
    out = capsys.readouterr().out
    assert "dependencies  already" in out
    assert "oracle        already" in out
    assert calls.names() == []


def test_yes_skips_every_prompt(probes, calls, monkeypatch):
    """--yes drives the whole pipeline unattended, and the later steps see
    the state the earlier ones produced (here: Forge/Java now installed)."""
    from commander_builder import bootstrap
    probes["deps"] = FakeStatus(forge=False, jre=False, cards=False)
    probes["decks"] = (12, True)

    def install(name, **flip):
        inner = calls.record(name)

        def fn(*args, **kwargs):
            probes["deps"] = FakeStatus(**flip)
            return inner(*args, **kwargs)
        return fn

    monkeypatch.setattr(bootstrap, "download_forge",
                        install("download_forge", forge=True, jre=False))
    monkeypatch.setattr(bootstrap, "ensure_jre",
                        install("ensure_jre", forge=True, jre=True))

    def no_prompts(prompt):  # pragma: no cover — asserted not to run
        raise AssertionError("--yes must not prompt")

    monkeypatch.setattr(init_cli, "_read_line", no_prompts)
    rc = init_cli.run_init(init_cli.InitOptions(assume_yes=True))
    assert rc == 0
    assert calls.names() == [
        "download_forge", "ensure_jre", "oracle_main", "curate_main",
    ]


def test_yes_defaults_deck_acquisition_to_harvest(probes, calls, monkeypatch):
    probes["deps"] = FakeStatus(forge=True, jre=True, cards=True)
    probes["oracle"] = (35000, True)
    monkeypatch.setattr(init_cli, "_read_line",
                        lambda prompt: pytest.fail("--yes must not prompt"))
    init_cli.run_init(init_cli.InitOptions(assume_yes=True))
    assert ("import_main", (["--harvest", "3"],), {}) in calls.log


# --------------------------------------------------------------------------- #
# Ordering + resumability end-to-end
# --------------------------------------------------------------------------- #
def test_steps_run_in_dependency_order(probes, calls):
    probes["deps"] = FakeStatus(forge=False, jre=False, cards=False)
    probes["decks"] = (0, False)
    init_cli.run_init(init_cli.InitOptions(assume_yes=True, decks="harvest"))
    # deps -> oracle -> decks -> (pool skipped: probes still report 0
    # candidates, since the fake harvest wrote nothing)
    assert calls.names() == [
        "download_forge", "ensure_jre", "oracle_main", "import_main",
    ]


def test_rerun_after_everything_is_done_calls_nothing(probes, calls, capsys):
    probes["deps"] = FakeStatus(forge=True, jre=True, cards=True)
    probes["oracle"] = (35000, True)
    probes["decks"] = (30, True)
    probes["pool"] = (Path("/pools/B3.json"), True)
    rc = init_cli.run_init(init_cli.InitOptions())
    assert rc == 0
    assert calls.names() == []
    out = capsys.readouterr().out
    assert out.count("already") >= 4


def test_failed_step_does_not_abort_the_rest(probes, calls, monkeypatch):
    from commander_builder import oracle_store
    probes["decks"] = (12, True)
    monkeypatch.setattr(oracle_store, "main", lambda argv: 2)
    rc = init_cli.run_init(init_cli.InitOptions(assume_yes=True))
    assert rc == 1
    assert "curate_main" in calls.names()  # step 4 still got its turn


# --------------------------------------------------------------------------- #
# argparse surface
# --------------------------------------------------------------------------- #
def test_main_passes_flags_through(probes, calls, monkeypatch):
    seen = {}

    def fake_run(opts):
        seen["opts"] = opts
        return 0

    monkeypatch.setattr(init_cli, "run_init", fake_run)
    assert init_cli.main(["--bracket", "5", "--yes", "--decks", "premade"]) == 0
    opts = seen["opts"]
    assert (opts.bracket, opts.assume_yes, opts.decks) == (5, True, "premade")
    assert opts.dry_run is False


def test_main_rejects_out_of_range_bracket(capsys):
    assert init_cli.main(["--bracket", "9"]) == 2
    assert "out of range" in capsys.readouterr().out


def test_main_dry_run_end_to_end_is_offline(probes, calls):
    assert init_cli.main(["--dry-run"]) == 0
    assert calls.names() == []


def test_help_documents_the_expensive_flags(capsys):
    with pytest.raises(SystemExit) as exc:
        init_cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--dry-run" in out and "--yes" in out and "--decks" in out
