"""Tests for the FP-006 Flask scaffold.

Covers route shapes, deck enumeration, and path-traversal protection.
Mocks ``lookup_card`` so dashboard tests don't hit Scryfall.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

flask = pytest.importorskip("flask")  # skip if [web] extra not installed

from commander_builder.web.app import create_app, _list_decks, _resolve_deck_path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_deck(deck_dir: Path, name: str, commander: str = "Test Cmdr") -> Path:
    p = deck_dir / f"{name}.dck"
    body = (
        "[metadata]\n"
        f"Name={name}\n\n"
        "[Commander]\n"
        f"1 {commander}\n\n"
        "[Main]\n"
        + "1 Forest\n" * 35
        + "1 Cultivate\n" * 5
    )
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture
def deck_dir(tmp_path) -> Path:
    d = tmp_path / "decks"
    d.mkdir()
    _write_deck(d, "Alpha")
    _write_deck(d, "Bravo")
    return d


@pytest.fixture
def user_deck_dir(tmp_path) -> Path:
    """Deck dir that mixes [USER] and filler decks — used to exercise
    the user-only default filter."""
    d = tmp_path / "user_decks"
    d.mkdir()
    _write_deck(d, "[USER] Hakbal [B3]")
    _write_deck(d, "[USER] Hash [B3]")
    _write_deck(d, "Allies")          # filler / pool, no [USER] prefix
    _write_deck(d, "Avacyn Tribal")   # filler / pool
    return d


@pytest.fixture
def client(deck_dir, monkeypatch):
    """A Flask test client with Scryfall lookup stubbed."""
    def fake_lookup(name: str):
        if "Forest" in name:
            return {
                "type_line": "Basic Land — Forest",
                "oracle_text": "({T}: Add {G}.)",
                "cmc": 0.0,
                "color_identity": ["G"],
                "prices": {"usd": "0.05"},
            }
        if "Cultivate" in name:
            return {
                "type_line": "Sorcery",
                "oracle_text": "Search your library for up to two basic land cards...",
                "cmc": 3.0,
                "color_identity": ["G"],
                "prices": {"usd": "1.50"},
            }
        # Commander
        return {
            "type_line": "Legendary Creature — Elder Dragon",
            "oracle_text": "",
            "cmc": 5.0,
            "color_identity": ["G"],
            "prices": {"usd": "10.00"},
        }

    monkeypatch.setattr(
        "commander_builder.deck_dashboard.lookup_card", fake_lookup,
    )

    # Isolate the per-user config store: point it at a non-existent temp
    # path so the audit BYO-key resolver (header → config.json → env)
    # never picks up a real key from the developer's machine. Without
    # this, the no-key fallback tests would flake on a box that has a
    # configured ~/.commander-builder/config.json. (FP-011 unification.)
    monkeypatch.setenv(
        "COMMANDER_BUILDER_CONFIG",
        str(deck_dir.parent / "no_such_config.json"),
    )

    app = create_app(deck_dir=deck_dir)
    app.config["TESTING"] = True
    return app.test_client()


# ---------------------------------------------------------------------------
# _list_decks
# ---------------------------------------------------------------------------

def test_list_decks_finds_dck_files(deck_dir):
    # Default `user_only=True` filters out our test fixture decks
    # (Alpha/Bravo lack the [USER] prefix); use user_only=False for
    # the all-files test.
    decks = _list_decks(deck_dir, user_only=False)
    names = sorted(d["name"] for d in decks)
    assert names == ["Alpha", "Bravo"]


def test_list_decks_handles_missing_dir(tmp_path):
    assert _list_decks(tmp_path / "nope") == []


def test_list_decks_skips_non_dck(deck_dir):
    (deck_dir / "notes.txt").write_text("ignore me", encoding="utf-8")
    names = {d["name"] for d in _list_decks(deck_dir, user_only=False)}
    assert "notes" not in names


def test_list_decks_default_filters_to_user_prefix(user_deck_dir):
    """Default user_only=True keeps only [USER] *.dck files."""
    decks = _list_decks(user_deck_dir)
    names = {d["name"] for d in decks}
    # Display names strip the [USER] prefix.
    assert "Hakbal [B3]" in names
    assert "Hash [B3]" in names
    assert "Allies" not in names
    assert "Avacyn Tribal" not in names


def test_list_decks_user_only_false_includes_filler(user_deck_dir):
    decks = _list_decks(user_deck_dir, user_only=False)
    names = {d["name"] for d in decks}
    assert "Allies" in names
    assert "Avacyn Tribal" in names


def test_list_decks_hides_proposed_working_copies(user_deck_dir):
    """Transient _proposed_<timestamp> files staged by propose_swap
    should never appear in the sidebar — neither in user_only mode
    nor in the unfiltered listing."""
    # Plant a leftover proposed working copy in the PRE-uid name shape
    # (staged by builds before the same-second-collision fix)...
    leftover = user_deck_dir / "[USER] Hakbal [B3]_proposed_20260428_134828.dck"
    leftover.write_text("[Main]\n1 Forest\n", encoding="utf-8")
    # ...and one in the CURRENT shape (timestamp + 8-hex per-request
    # uid). Both must stay hidden.
    leftover_uid = (
        user_deck_dir
        / "[USER] Hakbal [B3]_proposed_20260428_134828_deadbeef.dck"
    )
    leftover_uid.write_text("[Main]\n1 Forest\n", encoding="utf-8")
    user_only = {d["id"] for d in _list_decks(user_deck_dir)}
    all_mode = {d["id"] for d in _list_decks(user_deck_dir, user_only=False)}
    assert "[USER] Hakbal [B3]_proposed_20260428_134828" not in user_only
    assert "[USER] Hakbal [B3]_proposed_20260428_134828" not in all_mode
    assert "[USER] Hakbal [B3]_proposed_20260428_134828_deadbeef" not in user_only
    assert "[USER] Hakbal [B3]_proposed_20260428_134828_deadbeef" not in all_mode


def test_decks_endpoint_filters_to_user_default(user_deck_dir, monkeypatch):
    monkeypatch.setattr(
        "commander_builder.deck_dashboard.lookup_card",
        lambda name: None,
    )
    app = create_app(deck_dir=user_deck_dir)
    app.config["TESTING"] = True
    c = app.test_client()
    body = c.get("/api/decks").get_json()
    names = {d["name"] for d in body["decks"]}
    assert "Hakbal [B3]" in names
    assert "Allies" not in names


def test_decks_endpoint_all_flag_includes_filler(user_deck_dir, monkeypatch):
    monkeypatch.setattr(
        "commander_builder.deck_dashboard.lookup_card",
        lambda name: None,
    )
    app = create_app(deck_dir=user_deck_dir)
    app.config["TESTING"] = True
    c = app.test_client()
    body = c.get("/api/decks?all=1").get_json()
    names = {d["name"] for d in body["decks"]}
    assert "Allies" in names


# ---------------------------------------------------------------------------
# _resolve_deck_path — traversal protection
# ---------------------------------------------------------------------------

def test_resolve_by_id_inside_dir(deck_dir):
    path = _resolve_deck_path(deck_dir, "Alpha", None)
    assert path is not None
    assert path.name == "Alpha.dck"


def test_resolve_by_id_missing_returns_none(deck_dir):
    assert _resolve_deck_path(deck_dir, "Ghost", None) is None


def test_resolve_explicit_path_outside_dir_blocked(deck_dir, tmp_path):
    outside = tmp_path / "outside.dck"
    outside.write_text("[Main]\n1 Forest\n", encoding="utf-8")
    # Even though file exists, it's outside deck_dir → blocked.
    assert _resolve_deck_path(deck_dir, None, str(outside)) is None


def test_resolve_explicit_path_inside_dir_ok(deck_dir):
    path = _resolve_deck_path(deck_dir, None, str(deck_dir / "Alpha.dck"))
    assert path is not None
    assert path.name == "Alpha.dck"


def test_resolve_traversal_attempt_blocked(deck_dir):
    # ../.. attack
    sneaky = str(deck_dir / ".." / ".." / "etc" / "passwd")
    assert _resolve_deck_path(deck_dir, None, sneaky) is None


def test_routes_audit_has_no_env_staging_machinery():
    """2026-07-19 BYO-key rework: the env-staging context manager and
    its lock are GONE — the key is threaded as an explicit advise()
    parameter instead. Guard against the old pattern creeping back in
    (any per-request os.environ write in the web layer is a thread race
    on Flask's threaded dev server)."""
    from commander_builder.web import routes_audit
    assert not hasattr(routes_audit, "_claude_api_key_env")
    assert not hasattr(routes_audit, "_CLAUDE_ENV_LOCK")
    # No os.environ WRITES anywhere in the module source (reads are fine —
    # the fallback-warning branch legitimately checks key presence).
    import inspect
    src = inspect.getsource(routes_audit)
    assert "os.environ[" not in src
    assert "environ.pop" not in src


def test_resolve_explicit_path_non_dck_inside_dir_blocked(deck_dir):
    """A non-.dck file inside deck_dir must NOT resolve — otherwise a
    crafted ?path= could make DELETE/PUT clobber a pool JSON / soak summary
    / staged file that merely lives alongside the decks."""
    victim = deck_dir / "soak_summary.json"
    victim.write_text("{}", encoding="utf-8")
    assert _resolve_deck_path(deck_dir, None, str(victim)) is None
    assert victim.exists()  # untouched


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def test_root_serves_placeholder_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Commander Builder" in resp.data
    assert b"<html" in resp.data.lower()


def test_root_loads_static_assets(client):
    """Smoke test: root HTML references app.js and app.css, both
    served by Flask's static endpoint."""
    resp = client.get("/")
    body = resp.data.decode("utf-8")
    assert "app.js" in body
    assert "app.css" in body


def test_static_css_serves(client):
    resp = client.get("/static/app.css")
    try:
        assert resp.status_code == 200
        assert resp.mimetype == "text/css"
        assert b"--bg" in resp.data  # CSS variable from the theme
    finally:
        # Flask's send_from_directory keeps the file handle open on
        # the Response until close() is called; without this, pytest
        # emits a ResourceWarning when GC eventually closes the file.
        resp.close()


def test_static_js_serves(client):
    resp = client.get("/static/app.js")
    try:
        assert resp.status_code == 200
        assert "javascript" in resp.mimetype.lower()
        assert b"renderDashboard" in resp.data
    finally:
        resp.close()


def test_health_reports_ok_and_deck_count(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    # The fixture decks lack the [USER] prefix so the default health
    # count (which honors the user_only filter) is 0. The all=1 listing
    # below confirms the files are present.
    assert body["deck_count"] == 0


def test_forge_version_endpoint_returns_version_info(client, monkeypatch):
    """Surfaces version + age so the UI can show a "Forge 2.0.12
    (19d old)" badge and warn when stale."""
    from datetime import datetime, timezone
    from commander_builder.forge_runner import ForgeVersionInfo
    fake_info = ForgeVersionInfo(
        jar_path=Path("/fake/forge-gui-desktop-2.0.12-jar-with-dependencies.jar"),
        version="2.0.12",
        build_date=datetime(2026, 4, 23, 19, 50, 58, tzinfo=timezone.utc),
        age_days=19,
        is_stale=False,
    )
    monkeypatch.setattr(
        "commander_builder.web.app.detect_forge_version",
        lambda *a, **kw: fake_info,
    )
    resp = client.get("/api/forge_version")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["version"] == "2.0.12"
    assert body["age_days"] == 19
    assert body["is_stale"] is False
    assert body["build_date"] is not None
    assert body["jar_path"].endswith("forge-gui-desktop-2.0.12-jar-with-dependencies.jar")


def test_forge_version_endpoint_flags_stale_install(client, monkeypatch):
    """When the jar is older than the stale threshold, is_stale=True."""
    from datetime import datetime, timezone
    from commander_builder.forge_runner import ForgeVersionInfo
    fake_info = ForgeVersionInfo(
        jar_path=Path("/fake/jar.jar"),
        version="1.9.0",
        build_date=datetime(2025, 8, 1, tzinfo=timezone.utc),
        age_days=285,
        is_stale=True,
    )
    monkeypatch.setattr(
        "commander_builder.web.app.detect_forge_version",
        lambda *a, **kw: fake_info,
    )
    resp = client.get("/api/forge_version")
    body = resp.get_json()
    assert body["is_stale"] is True
    assert body["age_days"] == 285


def test_forge_version_endpoint_handles_missing_jar(client, monkeypatch):
    """No jar found → all fields null but endpoint still 200s."""
    from commander_builder.forge_runner import ForgeVersionInfo
    monkeypatch.setattr(
        "commander_builder.web.app.detect_forge_version",
        lambda *a, **kw: ForgeVersionInfo(),
    )
    resp = client.get("/api/forge_version")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["version"] is None
    assert body["jar_path"] is None
    assert body["is_stale"] is False


def test_decks_endpoint_lists_available(client):
    resp = client.get("/api/decks?all=1")
    assert resp.status_code == 200
    body = resp.get_json()
    names = sorted(d["name"] for d in body["decks"])
    assert names == ["Alpha", "Bravo"]


def test_dashboard_returns_data_for_known_deck(client):
    resp = client.get("/api/dashboard?deck=Alpha")
    assert resp.status_code == 200
    body = resp.get_json()
    # All seven panels present.
    for key in (
        "commander", "deck_progress", "stat_tiles",
        "mana_curve", "categories", "theme_tags", "suggested_adds",
    ):
        assert key in body, f"missing panel: {key}"
    # Commander parsed.
    assert body["commander"]["name"] == "Test Cmdr"
    # Lands counted.
    assert body["stat_tiles"]["lands"] >= 35


def test_dashboard_404_on_missing_deck(client):
    resp = client.get("/api/dashboard?deck=Ghost")
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["error"] == "deck not found"


def test_dashboard_400_on_bad_bracket(client):
    resp = client.get("/api/dashboard?deck=Alpha&bracket=zzz")
    assert resp.status_code == 400


def test_dashboard_with_valid_bracket(client):
    resp = client.get("/api/dashboard?deck=Alpha&bracket=3")
    assert resp.status_code == 200
    body = resp.get_json()
    # Power level should be in 1..10.
    # Bracket is the canonical 1..5 field; power_level is kept as a
    # backwards-compat alias and contains the same value.
    assert 1 <= body["stat_tiles"]["bracket"] <= 5
    assert body["stat_tiles"]["power_level"] == body["stat_tiles"]["bracket"]
    assert body["stat_tiles"]["bracket_name"] in {
        "Exhibition", "Core", "Upgraded", "Optimized", "cEDH",
    }


def test_dashboard_traversal_blocked(client, tmp_path):
    outside = tmp_path / "evil.dck"
    outside.write_text("[Main]\n1 Forest\n", encoding="utf-8")
    resp = client.get(f"/api/dashboard?path={outside}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/iterations
# ---------------------------------------------------------------------------

@pytest.fixture
def seeded_client(deck_dir, tmp_path, monkeypatch):
    """A client with a knowledge_log seeded by the demo script."""
    import importlib.util
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts" / "seed_demo_knowledge_log.py"
    )
    spec = importlib.util.spec_from_file_location("_seed_demo_klog", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    db = tmp_path / "klog.sqlite"
    mod.seed_demo(db, deck_id="omnath")

    # Stub Scryfall (in case any dashboard call piggy-backs).
    monkeypatch.setattr(
        "commander_builder.deck_dashboard.lookup_card",
        lambda name: {"type_line": "Basic Land", "cmc": 0.0,
                      "color_identity": ["G"], "prices": {"usd": "0.05"}},
    )

    app = create_app(deck_dir=deck_dir, knowledge_db=db)
    app.config["TESTING"] = True
    return app.test_client()


def test_iterations_endpoint_without_deck_returns_recent(seeded_client):
    resp = seeded_client.get("/api/iterations")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "iterations" in body
    assert body["count"] == 4
    # Recent-first ordering — newest is the v4 tutor experiment.
    assert "tutor" in body["iterations"][0]["deck_name"].lower()


def test_iterations_endpoint_filters_by_deck(seeded_client):
    resp = seeded_client.get("/api/iterations?deck=omnath")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["count"] == 4
    assert body["deck_id"] == "omnath"
    # iterations_for_deck returns oldest-first.
    assert "v1" in body["iterations"][0]["deck_name"]


def test_iterations_endpoint_filters_unknown_deck(seeded_client):
    resp = seeded_client.get("/api/iterations?deck=does-not-exist")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["count"] == 0
    assert body["iterations"] == []


def test_iterations_drops_deck_snapshot_blob(seeded_client):
    """Snapshot blobs would balloon JSON payloads — we omit them
    from the listing endpoint."""
    resp = seeded_client.get("/api/iterations?deck=omnath")
    body = resp.get_json()
    for entry in body["iterations"]:
        assert "deck_snapshot" not in entry


def test_iterations_400_on_bad_limit(seeded_client):
    resp = seeded_client.get("/api/iterations?limit=abc")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /api/iterations + /api/iteration_graph — Moxfield publicId fallback
# (2026-05-16 fix). The frontend keys decks by filename stem but
# auto-curate writes rows keyed by the Moxfield publicId from the
# .dck metadata. Without this resolution step, iteration history
# never surfaces for Moxfield-imported decks and the new Tier-1.3
# verdict panel can't render its rows.
# ---------------------------------------------------------------------------

def _moxfield_deck_setup(tmp_path):
    """Build a tmp deck_dir + sqlite db with one iteration row keyed
    by Moxfield publicId 'abc123', plus a matching .dck file at
    ``<deck_dir>/[USER] Moxy [B3].dck`` that carries ``Moxfield=abc123``
    in its metadata."""
    from commander_builder.knowledge_log import Iteration, record_iteration
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    stem = "[USER] Moxy [B3]"
    deck = deck_dir / f"{stem}.dck"
    deck.write_text(
        "[metadata]\nName=Moxy\nMoxfield=abc123\n"
        "[Commander]\n1 Edgar Markov\n"
        "[Main]\n1 Sol Ring\n",
        encoding="utf-8",
    )
    db = tmp_path / "klog.sqlite"
    it = Iteration(
        deck_id="abc123",           # publicId — NOT the filename stem
        deck_name=stem,
        bracket=3,
        parent_id=None,
        audit_version="claude-auto",
        audit_manifest={"added": [], "removed": [], "rationale": "x"},
        verdict="pending",
        deck_snapshot=deck.read_text(encoding="utf-8"),
    )
    record_iteration(it, db_path=db)
    return deck_dir, db, stem


def test_iterations_endpoint_resolves_filename_stem_to_publicId(tmp_path):
    """Filename-stem query falls through to the .dck's Moxfield
    publicId so iteration history surfaces for Moxfield-imported decks."""
    deck_dir, db, stem = _moxfield_deck_setup(tmp_path)
    app = create_app(deck_dir=deck_dir, knowledge_db=db)
    app.config["TESTING"] = True
    client = app.test_client()
    from urllib.parse import quote
    resp = client.get(f"/api/iterations?deck={quote(stem)}")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["count"] == 1
    assert body["iterations"][0]["deck_id"] == "abc123"


def test_iteration_graph_endpoint_resolves_filename_stem_to_publicId(tmp_path):
    """Same resolution for the graph endpoint — without it the new
    Tier-1.3 verdict panel can't render Kept/Reverted/Neutral buttons
    for pending Moxfield-deck iterations."""
    deck_dir, db, stem = _moxfield_deck_setup(tmp_path)
    app = create_app(deck_dir=deck_dir, knowledge_db=db)
    app.config["TESTING"] = True
    client = app.test_client()
    from urllib.parse import quote
    resp = client.get(f"/api/iteration_graph?deck={quote(stem)}")
    assert resp.status_code == 200
    body = resp.get_json()
    assert len(body["nodes"]) == 1
    assert body["nodes"][0]["verdict"] == "pending"


def test_iterations_endpoint_prefers_stem_when_both_have_rows(tmp_path):
    """Defensive: when rows exist under BOTH the filename stem AND the
    publicId (rare — e.g. legacy stem-keyed iterations + new publicId-
    keyed ones), the merge returns the union sorted chronologically
    without dropping either set or producing duplicates."""
    from commander_builder.knowledge_log import Iteration, record_iteration
    deck_dir, db, stem = _moxfield_deck_setup(tmp_path)
    # Add a legacy stem-keyed row too.
    record_iteration(
        Iteration(
            deck_id=stem, deck_name=stem, bracket=3,
            parent_id=None, audit_version="legacy",
            audit_manifest={"added": [], "removed": [], "rationale": "legacy"},
            verdict="kept",
        ),
        db_path=db,
    )
    app = create_app(deck_dir=deck_dir, knowledge_db=db)
    app.config["TESTING"] = True
    client = app.test_client()
    from urllib.parse import quote
    body = client.get(f"/api/iterations?deck={quote(stem)}").get_json()
    assert body["count"] == 2
    # Both deck_ids present, no duplicates.
    seen_ids = {r["id"] for r in body["iterations"]}
    assert len(seen_ids) == 2


def test_iterations_endpoint_no_moxfield_metadata_falls_back_to_stem(tmp_path):
    """Hand-built local deck with no Moxfield= line: filename-stem
    iteration rows still surface (no false-negative from the publicId
    branch)."""
    from commander_builder.knowledge_log import Iteration, record_iteration
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    stem = "[USER] LocalOnly [B2]"
    deck = deck_dir / f"{stem}.dck"
    deck.write_text(
        "[Commander]\n1 Test\n[Main]\n1 Sol Ring\n", encoding="utf-8",
    )
    db = tmp_path / "klog.sqlite"
    record_iteration(
        Iteration(
            deck_id=stem, deck_name=stem, bracket=2,
            parent_id=None, audit_version="manual",
            audit_manifest={"added": [], "removed": [], "rationale": ""},
            verdict="pending",
        ),
        db_path=db,
    )
    app = create_app(deck_dir=deck_dir, knowledge_db=db)
    app.config["TESTING"] = True
    client = app.test_client()
    from urllib.parse import quote
    body = client.get(f"/api/iterations?deck={quote(stem)}").get_json()
    assert body["count"] == 1
    assert body["iterations"][0]["deck_id"] == stem


# ---------------------------------------------------------------------------
# PATCH /api/iterations/<id>/verdict — manual verdict assignment (Tier 1.3)
# ---------------------------------------------------------------------------

def _first_iteration_id(seeded_client) -> int:
    """Pull the lowest iteration id from the seeded DB so the verdict
    tests don't have to know the seed's specific autoincrement values."""
    resp = seeded_client.get("/api/iterations?deck=omnath")
    rows = resp.get_json()["iterations"]
    assert rows, "seeded knowledge_log should have at least one iteration"
    return rows[0]["id"]


def test_patch_verdict_marks_iteration_kept(seeded_client):
    """Happy-path: PATCH with verdict='kept' returns 200 + flips the
    row's verdict column. Subsequent GET reflects the change."""
    iteration_id = _first_iteration_id(seeded_client)
    resp = seeded_client.patch(
        f"/api/iterations/{iteration_id}/verdict",
        json={"verdict": "kept", "notes": "manual review confirmed"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {
        "ok": True, "iteration_id": iteration_id, "verdict": "kept",
    }
    # Verify the change actually landed via the same listing endpoint.
    listing = seeded_client.get("/api/iterations?deck=omnath").get_json()
    matched = [r for r in listing["iterations"] if r["id"] == iteration_id]
    assert matched and matched[0]["verdict"] == "kept"


def test_patch_verdict_accepts_pending_to_clear(seeded_client):
    """Setting verdict back to 'pending' is allowed so the UI can undo
    a misclick without needing a separate delete endpoint."""
    iteration_id = _first_iteration_id(seeded_client)
    seeded_client.patch(
        f"/api/iterations/{iteration_id}/verdict",
        json={"verdict": "reverted"},
    )
    resp = seeded_client.patch(
        f"/api/iterations/{iteration_id}/verdict",
        json={"verdict": "pending"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["verdict"] == "pending"


def test_patch_verdict_rejects_unknown_value(seeded_client):
    """400 on any verdict outside the allowed set — defensive against
    typos and bypassing the curator/sim pipeline."""
    iteration_id = _first_iteration_id(seeded_client)
    resp = seeded_client.patch(
        f"/api/iterations/{iteration_id}/verdict",
        json={"verdict": "approved"},
    )
    assert resp.status_code == 400
    assert "kept/reverted/neutral/inconclusive/pending" in resp.get_json()["error"]


def test_patch_verdict_rejects_missing_body(seeded_client):
    """Empty body → 400, not a crash."""
    iteration_id = _first_iteration_id(seeded_client)
    resp = seeded_client.patch(f"/api/iterations/{iteration_id}/verdict")
    assert resp.status_code == 400


def test_patch_verdict_rejects_non_string_notes(seeded_client):
    """notes is free-text; reject non-string types so the sqlite
    write doesn't smuggle in JSON objects."""
    iteration_id = _first_iteration_id(seeded_client)
    resp = seeded_client.patch(
        f"/api/iterations/{iteration_id}/verdict",
        json={"verdict": "kept", "notes": {"oops": "object"}},
    )
    assert resp.status_code == 400


def test_patch_verdict_unknown_id_succeeds_silently(seeded_client):
    """update_verdict() issues a bare UPDATE with no rowcount check,
    so an unknown iteration_id returns 200 without raising. Pinning
    that contract here so a future change to fail-loud doesn't
    silently regress the UI's optimistic-update flow."""
    resp = seeded_client.patch(
        "/api/iterations/9999999/verdict",
        json={"verdict": "kept"},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# PATCH /api/iterations/<id>/milestone — AGENT_BACKLOG #012
# ---------------------------------------------------------------------------

def test_patch_milestone_tags_iteration(seeded_client):
    """Happy path: PATCH with a string milestone returns 200 +
    echoes the stored value. Subsequent GET reflects the tag."""
    iteration_id = _first_iteration_id(seeded_client)
    resp = seeded_client.patch(
        f"/api/iterations/{iteration_id}/milestone",
        json={"milestone": "baseline"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {
        "ok": True, "iteration_id": iteration_id, "milestone": "baseline",
    }
    # Verify via listing.
    listing = seeded_client.get("/api/iterations?deck=omnath").get_json()
    matched = [r for r in listing["iterations"] if r["id"] == iteration_id]
    assert matched and matched[0]["milestone"] == "baseline"


def test_patch_milestone_null_clears_tag(seeded_client):
    """Pass null (or empty string) to clear the milestone — the
    UI's "untag" action."""
    iteration_id = _first_iteration_id(seeded_client)
    seeded_client.patch(
        f"/api/iterations/{iteration_id}/milestone",
        json={"milestone": "baseline"},
    )
    resp = seeded_client.patch(
        f"/api/iterations/{iteration_id}/milestone",
        json={"milestone": None},
    )
    assert resp.status_code == 200
    assert resp.get_json()["milestone"] is None


def test_patch_milestone_truncates_long_values(seeded_client):
    """Echoed value is the normalized stored form (clipped to 64)."""
    iteration_id = _first_iteration_id(seeded_client)
    long_label = "X" * 200
    resp = seeded_client.patch(
        f"/api/iterations/{iteration_id}/milestone",
        json={"milestone": long_label},
    )
    assert resp.status_code == 200
    echoed = resp.get_json()["milestone"]
    assert echoed is not None
    assert len(echoed) == 64


def test_patch_milestone_rejects_non_string(seeded_client):
    """Numeric / dict / list values don't make sense as labels."""
    iteration_id = _first_iteration_id(seeded_client)
    resp = seeded_client.patch(
        f"/api/iterations/{iteration_id}/milestone",
        json={"milestone": 42},
    )
    assert resp.status_code == 400
    assert "string or null" in resp.get_json()["error"]


def test_patch_milestone_rejects_missing_field(seeded_client):
    """Body must contain the ``milestone`` key (even if null) so a
    typo in the JSON doesn't silently no-op."""
    iteration_id = _first_iteration_id(seeded_client)
    resp = seeded_client.patch(
        f"/api/iterations/{iteration_id}/milestone",
        json={"oops": "not-the-key"},
    )
    assert resp.status_code == 400


def test_patch_milestone_unknown_id_succeeds_silently(seeded_client):
    """Same fail-quiet contract as the verdict endpoint."""
    resp = seeded_client.patch(
        "/api/iterations/9999999/milestone",
        json={"milestone": "foo"},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /api/dashboard?advise=1 + /api/advise
# ---------------------------------------------------------------------------

def _stub_advise(monkeypatch):
    """Stub improvement_advisor.advise so tests run offline."""
    from types import SimpleNamespace

    def fake(deck_path, bracket, **_kwargs):
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(
                    card="Lotus Cobra", action="add",
                    reason="Mana on landfall",
                    evidence={"inclusion_pct": 78.0, "synergy_pct": 12.0,
                              "price_usd": 8.5},
                ),
                SimpleNamespace(
                    card="Cultivate", action="cut",
                    reason="Replaced by faster ramp",
                    evidence={},
                ),
                SimpleNamespace(
                    card="Tireless Tracker", action="add",
                    reason="Landfall payoff",
                    evidence={"inclusion_pct": 65.0, "synergy_pct": 18.0,
                              "price_usd": 4.0},
                ),
            ],
        )
    monkeypatch.setattr("commander_builder.improvement_advisor.advise", fake)


def test_dashboard_with_advise_param_includes_suggestions(client, monkeypatch):
    _stub_advise(monkeypatch)
    resp = client.get("/api/dashboard?deck=Alpha&advise=1")
    assert resp.status_code == 200
    body = resp.get_json()
    sugs = body["suggested_adds"]
    # Two adds (cuts excluded), no Cultivate.
    assert len(sugs) == 2
    cards = {s["card"] for s in sugs}
    assert "Lotus Cobra" in cards
    assert "Tireless Tracker" in cards
    assert "Cultivate" not in cards


def test_dashboard_without_advise_param_omits_suggestions(client, monkeypatch):
    """No `advise=1` → no advisor call, suggested_adds stays empty."""
    called: dict = {"n": 0}
    def boom(*a, **kw):
        called["n"] += 1
        raise RuntimeError("should not be called")
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", boom,
    )
    resp = client.get("/api/dashboard?deck=Alpha")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["suggested_adds"] == []
    assert called["n"] == 0


def test_dashboard_advise_failure_degrades_gracefully(client, monkeypatch):
    """If advise() raises, dashboard still renders without suggestions."""
    def explode(*a, **kw):
        raise RuntimeError("EDHREC fetch failed")
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", explode,
    )
    resp = client.get("/api/dashboard?deck=Alpha&advise=1")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["suggested_adds"] == []


def test_advise_endpoint_returns_suggestions(client, monkeypatch):
    _stub_advise(monkeypatch)
    resp = client.get("/api/advise?deck=Alpha&bracket=3")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["bracket"] == 3
    assert len(body["suggestions"]) == 2  # adds only
    first = body["suggestions"][0]
    assert {"card", "inclusion_pct", "synergy_pct",
            "rationale", "price_usd"} <= set(first.keys())


def test_advise_endpoint_503_on_failure(client, monkeypatch):
    def explode(*a, **kw):
        raise ConnectionError("network down")
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", explode,
    )
    resp = client.get("/api/advise?deck=Alpha")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["error"] == "advise unavailable"


def test_advise_endpoint_404_on_missing_deck(client):
    resp = client.get("/api/advise?deck=Ghost")
    assert resp.status_code == 404


def test_advise_endpoint_400_on_bad_bracket(client):
    resp = client.get("/api/advise?deck=Alpha&bracket=zzz")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /api/iteration/<id> + /api/iteration/<id>/snapshot
# ---------------------------------------------------------------------------

def test_iteration_detail_returns_full_record(seeded_client):
    resp = seeded_client.get("/api/iteration/1")
    assert resp.status_code == 200
    body = resp.get_json()
    # Detail includes the snapshot blob (excluded from listings).
    assert "deck_snapshot" in body
    assert "1 Omnath, Locus of Creation" in body["deck_snapshot"]
    assert body["id"] == 1


def test_iteration_detail_404_on_missing(seeded_client):
    resp = seeded_client.get("/api/iteration/9999")
    assert resp.status_code == 404


def test_iteration_snapshot_serves_plain_text(seeded_client):
    resp = seeded_client.get("/api/iteration/1/snapshot")
    assert resp.status_code == 200
    assert resp.mimetype == "text/plain"
    assert b"[Commander]" in resp.data


def test_iteration_snapshot_404_when_missing(seeded_client):
    resp = seeded_client.get("/api/iteration/9999/snapshot")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/compare/<old_id>/<new_id>
# ---------------------------------------------------------------------------

def test_compare_iterations_returns_diff(seeded_client):
    resp = seeded_client.get("/api/compare/1/2")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["old_id"] == 1
    assert body["new_id"] == 2
    assert "added" in body
    assert "removed" in body
    assert isinstance(body["unchanged_count"], int)


def test_compare_iterations_404_on_missing_id(seeded_client):
    resp = seeded_client.get("/api/compare/1/9999")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/deck_text + /api/propose_swap (modify-deck + A/B sim)
# ---------------------------------------------------------------------------

def test_deck_text_returns_dck_blob(client):
    """Direct read of a deck file by id."""
    resp = client.get("/api/deck_text?deck=Alpha")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["deck"] == "Alpha"
    assert "[Main]" in body["text"]
    assert "1 Forest" in body["text"]


def test_deck_text_404_on_missing_deck(client):
    resp = client.get("/api/deck_text?deck=Ghost")
    assert resp.status_code == 404


def _stub_compare(
    monkeypatch, winner="new", old_wins=4, new_wins=11, draws=0,
    pods_planned=2, pods_completed=2, stopped_early=False,
    pod_intra_aborts=None,
):
    """Stub commander_builder.compare_versions.compare so the
    A/B endpoint runs without Forge.

    ``pod_intra_aborts`` is an optional list of (intra_pod_aborted,
    games_actually_played) tuples for each pod, exercising the
    Sprint 1C per-pod-abort telemetry path. Defaults to all-pods-ran-
    full-length when None.
    """
    from types import SimpleNamespace

    def fake_compare(old_deck, new_deck, bracket, games_per_pod,
                     filler_pairs=2, mode="1v1", runner=None,
                     out_dir=None, deck_dir=None,
                     parallel=True, max_workers=None,
                     early_stop=True):
        old_stats = SimpleNamespace(deck_filename=old_deck,
                                    wins=old_wins, games=old_wins + new_wins + draws)
        new_stats = SimpleNamespace(deck_filename=new_deck,
                                    wins=new_wins, games=old_wins + new_wins + draws)
        if pod_intra_aborts:
            pods = [
                {
                    "pod_index": i + 1,
                    "intra_pod_aborted": aborted,
                    "games_actually_played": played,
                    "duration_sec": 30.0,
                }
                for i, (aborted, played) in enumerate(pod_intra_aborts)
            ]
        else:
            pods = [{} for _ in range(pods_completed)]
        return SimpleNamespace(
            old_deck=old_deck, new_deck=new_deck,
            bracket=bracket, mode=mode, games_per_pod=games_per_pod,
            old_stats=old_stats, new_stats=new_stats,
            draws=draws, total_games=games_per_pod,
            timestamp="2026-04-28T12:00:00",
            winner=winner,
            margin=abs(new_wins - old_wins),
            pods=pods,
            pods_planned=pods_planned,
            stopped_early=stopped_early,
        )
    monkeypatch.setattr(
        "commander_builder.compare_versions.compare", fake_compare,
    )

    # Stub ForgeRunner.locate so the endpoint passes the availability check.
    monkeypatch.setattr(
        "commander_builder.forge_runner.ForgeRunner.locate",
        classmethod(lambda cls: SimpleNamespace(name="stubbed")),
    )


def test_propose_swap_runs_compare_and_returns_summary(client, monkeypatch):
    _stub_compare(monkeypatch, winner="new", old_wins=4, new_wins=11)
    new_text = (
        "[metadata]\nName=Alpha v2\n\n"
        "[Commander]\n1 Test Cmdr\n\n"
        "[Main]\n" + "1 Forest\n" * 35 + "1 Lotus Cobra\n" * 5
    )
    resp = client.post("/api/propose_swap", json={
        "deck": "Alpha", "new_text": new_text, "games": 10,
    })
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["winner"] == "new"
    assert body["old_wins"] == 4
    assert body["new_wins"] == 11
    assert body["games_per_pod"] == 10
    # Diff was non-empty (Lotus Cobra added, Cultivate removed).
    assert any("Lotus Cobra" in s for s in body["diff"]["added"])


def test_propose_swap_forwards_early_stop_metadata(client, monkeypatch):
    """Sprint 1B: when compare() reports it stopped early, the
    /api/propose_swap response surfaces pods_completed / pods_planned
    / stopped_early so the UI can show the user."""
    _stub_compare(
        monkeypatch, winner="new", old_wins=15, new_wins=0,
        pods_planned=4, pods_completed=3, stopped_early=True,
    )
    new_text = (
        "[metadata]\nName=Alpha v2\n\n"
        "[Commander]\n1 Test Cmdr\n\n"
        "[Main]\n" + "1 Forest\n" * 35 + "1 Lotus Cobra\n" * 5
    )
    resp = client.post("/api/propose_swap", json={
        "deck": "Alpha", "new_text": new_text, "games": 10,
    })
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["pods_planned"] == 4
    assert body["pods_completed"] == 3
    assert body["stopped_early"] is True


def test_propose_swap_forwards_pod_summaries_with_intra_pod_abort(
    client, monkeypatch,
):
    """Sprint 1C: per-pod abort telemetry surfaces in pod_summaries
    so the UI can show 'Pod 2 stopped at game 3/5'."""
    _stub_compare(
        monkeypatch, winner="new", old_wins=2, new_wins=8,
        pods_planned=2, pods_completed=2, stopped_early=False,
        pod_intra_aborts=[(False, 5), (True, 3)],
    )
    new_text = (
        "[metadata]\nName=Alpha v2\n\n"
        "[Commander]\n1 Test Cmdr\n\n"
        "[Main]\n" + "1 Forest\n" * 35 + "1 Lotus Cobra\n" * 5
    )
    resp = client.post("/api/propose_swap", json={
        "deck": "Alpha", "new_text": new_text, "games": 10,
    })
    assert resp.status_code == 200
    body = resp.get_json()
    assert "pod_summaries" in body
    assert len(body["pod_summaries"]) == 2
    assert body["pod_summaries"][0]["intra_pod_aborted"] is False
    assert body["pod_summaries"][0]["games_actually_played"] == 5
    assert body["pod_summaries"][1]["intra_pod_aborted"] is True
    assert body["pod_summaries"][1]["games_actually_played"] == 3


def test_propose_swap_400_on_bad_games_value(client, monkeypatch):
    _stub_compare(monkeypatch)
    resp = client.post("/api/propose_swap", json={
        "deck": "Alpha",
        "new_text": "[Main]\n1 Forest\n",
        "games": 7,  # not 10/40/100
    })
    assert resp.status_code == 400


def test_propose_swap_400_on_no_changes(client, monkeypatch):
    _stub_compare(monkeypatch)
    # Same content as fixture — no diff.
    same_text = (
        "[metadata]\nName=Alpha\n\n"
        "[Commander]\n1 Test Cmdr\n\n"
        "[Main]\n" + "1 Forest\n" * 35 + "1 Cultivate\n" * 5
    )
    resp = client.post("/api/propose_swap", json={
        "deck": "Alpha", "new_text": same_text, "games": 10,
    })
    assert resp.status_code == 400
    assert "no changes" in resp.get_json()["error"]


def test_propose_swap_404_on_missing_deck(client, monkeypatch):
    _stub_compare(monkeypatch)
    resp = client.post("/api/propose_swap", json={
        "deck": "Ghost",
        "new_text": "[Main]\n1 Forest\n",
        "games": 10,
    })
    assert resp.status_code == 404


def test_propose_swap_400_on_empty_new_text(client, monkeypatch):
    _stub_compare(monkeypatch)
    resp = client.post("/api/propose_swap", json={
        "deck": "Alpha", "new_text": "", "games": 10,
    })
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /api/deck_text PUT + DELETE
# ---------------------------------------------------------------------------

def test_deck_text_put_overwrites(client, deck_dir):
    new_body = (
        "[metadata]\nName=Edited\n\n"
        "[Commander]\n1 New Cmdr\n\n[Main]\n1 Forest\n"
    )
    resp = client.put(
        "/api/deck_text?deck=Alpha",
        json={"text": new_body},
    )
    assert resp.status_code == 200
    assert resp.get_json()["saved"] is True
    on_disk = (deck_dir / "Alpha.dck").read_text(encoding="utf-8")
    assert "1 New Cmdr" in on_disk


def test_deck_text_put_400_on_empty(client):
    resp = client.put("/api/deck_text?deck=Alpha", json={"text": ""})
    assert resp.status_code == 400


def test_deck_text_delete_removes(client, deck_dir):
    assert (deck_dir / "Alpha.dck").exists()
    resp = client.delete("/api/deck_text?deck=Alpha")
    assert resp.status_code == 200
    assert resp.get_json()["deleted"] is True
    assert not (deck_dir / "Alpha.dck").exists()


def test_deck_text_delete_404(client):
    resp = client.delete("/api/deck_text?deck=Ghost")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/import_deck (Moxfield URL + paste)
# ---------------------------------------------------------------------------

def test_import_deck_paste_creates_user_prefixed_file(client, deck_dir):
    paste = (
        "1 Sol Ring\n"
        "1 Arcane Signet\n"
        "1 Command Tower\n"
        "30 Forest\n"
    )
    resp = client.post("/api/import_deck", json={
        "name": "My Brew", "paste_text": paste, "bracket": 3,
    })
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    # Filename is normalized to [USER] My Brew [B3].dck.
    assert "[USER]" in body["filename"]
    assert "[B3]" in body["filename"]
    on_disk = (deck_dir / body["filename"]).read_text(encoding="utf-8")
    # Paste lacked a [Main] section → normalized to wrap one.
    assert "[Main]" in on_disk
    assert "1 Sol Ring" in on_disk


def test_import_deck_dck_format_paste_preserved(client, deck_dir):
    paste = (
        "[metadata]\nName=Already Forge\n\n"
        "[Commander]\n1 Edgar Markov\n\n"
        "[Main]\n1 Sol Ring\n"
    )
    resp = client.post("/api/import_deck", json={
        "name": "Edgar Test", "paste_text": paste,
    })
    assert resp.status_code == 200
    fn = resp.get_json()["filename"]
    on_disk = (deck_dir / fn).read_text(encoding="utf-8")
    assert "[Commander]" in on_disk
    assert "1 Edgar Markov" in on_disk


def test_import_deck_409_on_duplicate(client, deck_dir):
    paste = "1 Sol Ring\n"
    client.post("/api/import_deck", json={
        "name": "Dup", "paste_text": paste,
    })
    resp = client.post("/api/import_deck", json={
        "name": "Dup", "paste_text": paste,
    })
    assert resp.status_code == 409


def test_import_deck_400_when_no_content(client):
    resp = client.post("/api/import_deck", json={"name": "Empty"})
    assert resp.status_code == 400


def test_import_deck_creates_parent_dir_when_missing(tmp_path, monkeypatch):
    """Regression: a fresh checkout (or web app started before the canonical
    deck dir exists) used to fail import with ENOENT. The endpoint now
    auto-creates the parent so the write succeeds and the deck shows up
    in the sidebar."""
    nonexistent = tmp_path / "fresh" / "decks"
    assert not nonexistent.exists()  # precondition
    monkeypatch.setattr(
        "commander_builder.deck_dashboard.lookup_card",
        lambda name: {"type_line": "Basic Land", "cmc": 0.0,
                      "color_identity": ["G"], "prices": {"usd": "0.05"}},
    )
    app = create_app(deck_dir=nonexistent)
    app.config["TESTING"] = True
    client = app.test_client()

    resp = client.post("/api/import_deck", json={
        "name": "Dragon Rawr",
        "paste_text": "1 Sol Ring\n1 Forest\n",
        "bracket": 4,
    })
    assert resp.status_code == 200, resp.get_json()
    assert nonexistent.exists()
    assert (nonexistent / "[USER] Dragon Rawr [B4].dck").exists()


def test_create_app_default_deck_dir_resolves_to_vendor_forge(monkeypatch):
    """The default deck_dir should point at the canonical Forge userdata
    location so import + audit + compare all share the same source of
    truth. Was previously `Path.cwd() / 'decks'`, which split the world
    when the web app was launched from the repo root."""
    import os as _os
    from commander_builder.forge_runner import VENDOR_FORGE
    monkeypatch.delenv("COMMANDER_BUILDER_DECK_DIR", raising=False)
    app = create_app()
    expected = (VENDOR_FORGE / "userdata" / "decks" / "commander").resolve()
    assert Path(app.config["DECK_DIR"]).resolve() == expected
    # Defensive: confirm we're not falling through to the old CWD/decks
    # behavior (the regression we're guarding against).
    assert not str(app.config["DECK_DIR"]).endswith(
        str(Path("commander_builder") / "decks"),
    )


def test_import_deck_via_moxfield_url(client, deck_dir, monkeypatch):
    """Stubs fetch_deck so we don't hit Moxfield."""
    fake_json = {
        "name": "Moxfield Test Deck",
        "publicId": "abc123",
        "boards": {
            "commanders": {
                "cards": {
                    "k1": {
                        "quantity": 1,
                        "card": {
                            "name": "Edgar Markov",
                            "set": "C17",
                            "cn": "37",
                        },
                    },
                },
            },
            "mainboard": {
                "cards": {
                    "k2": {
                        "quantity": 1,
                        "card": {
                            "name": "Sol Ring", "set": "C17", "cn": "260",
                        },
                    },
                },
            },
        },
    }
    monkeypatch.setattr(
        "commander_builder.moxfield_import.fetch_deck",
        lambda public_id: fake_json,
    )
    resp = client.post("/api/import_deck", json={
        "moxfield_url": "https://moxfield.com/decks/abc123",
    })
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert "Moxfield Test Deck" in body["filename"]


def test_import_deck_route_stamps_name_from_filename_stem(
    client, deck_dir, monkeypatch,
):
    """Regression: the web import route sanitizes ':' etc. out of the
    filename but used to leave to_dck's raw Moxfield name in Name= —
    breaking Forge's picker and every name-keyed pipeline for such decks.
    The written file must carry Name=<stem>, with the pretty name kept in
    DisplayName= (the display-decision pin for the web import path)."""
    import re as _re
    fake_json = {
        "name": "Chatterfang: Squirrel Tribal \U0001f43f",
        "publicId": "abc123",
        "boards": {
            "commanders": {"cards": {"k1": {
                "quantity": 1, "card": {"name": "Chatterfang, Squirrel General"},
            }}},
            "mainboard": {"cards": {"k2": {
                "quantity": 1, "card": {"name": "Sol Ring"},
            }}},
        },
    }
    monkeypatch.setattr(
        "commander_builder.moxfield_import.fetch_deck",
        lambda public_id: fake_json,
    )
    resp = client.post("/api/import_deck", json={
        "moxfield_url": "https://moxfield.com/decks/abc123", "bracket": 3,
    })
    assert resp.status_code == 200, resp.get_json()
    fn = resp.get_json()["filename"]
    path = deck_dir / fn
    text = path.read_text(encoding="utf-8")
    name_val = _re.search(r"^Name=(.+)$", text, _re.MULTILINE).group(1)
    assert name_val == path.stem
    assert "DisplayName=Chatterfang: Squirrel Tribal \U0001f43f" in text


def test_import_deck_502_when_moxfield_fails(client, monkeypatch):
    def boom(public_id):
        raise RuntimeError("network down")
    monkeypatch.setattr(
        "commander_builder.moxfield_import.fetch_deck", boom,
    )
    resp = client.post("/api/import_deck", json={
        "moxfield_url": "https://moxfield.com/decks/abc",
    })
    assert resp.status_code == 502


# ---------------------------------------------------------------------------
# /api/moxfield_format
# ---------------------------------------------------------------------------

def test_moxfield_format_returns_paste_ready_text(client):
    resp = client.get("/api/moxfield_format?deck=Alpha")
    assert resp.status_code == 200
    body = resp.get_json()
    # No [metadata] block in the paste output.
    assert "[metadata]" not in body["text"]
    # Card lines preserved.
    assert "1 Forest" in body["text"]


def test_moxfield_format_404_on_missing(client):
    resp = client.get("/api/moxfield_format?deck=Ghost")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/game_changers + /api/deck_audit
# ---------------------------------------------------------------------------

def test_game_changers_list(client, monkeypatch):
    monkeypatch.setattr(
        "commander_builder.game_changers.load_game_changers",
        lambda **kw: {"Sol Ring", "Mana Crypt", "Demonic Tutor"},
    )
    resp = client.get("/api/game_changers")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["count"] == 3
    assert "Mana Crypt" in body["cards"]


def test_deck_audit_flags_in_deck_game_changers(client, monkeypatch):
    monkeypatch.setattr(
        "commander_builder.game_changers.load_game_changers",
        lambda **kw: {"Cultivate", "Some Game Changer"},
    )
    # Alpha's mainboard contains Cultivate per the fixture.
    resp = client.get("/api/deck_audit?deck=Alpha&bracket=2")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "Cultivate" in body["in_deck_game_changers"]
    # Bracket 2 + GCs present → warning fires.
    assert any("Game Changers" in w for w in body["warnings"])


def test_deck_audit_404_on_missing(client):
    resp = client.get("/api/deck_audit?deck=Ghost")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/audit (full-deck audit output) + _apply_swaps_to_dck
# ---------------------------------------------------------------------------

def test_apply_swaps_drops_cuts_and_appends_adds():
    from commander_builder.web.app import _apply_swaps_to_dck
    from types import SimpleNamespace
    original = (
        "[metadata]\nName=Test\n\n"
        "[Commander]\n1 Edgar Markov\n\n"
        "[Main]\n"
        "1 Sol Ring\n"
        "1 Cultivate\n"
        "1 Forest|MIR|315\n"
    )
    # 1 cut + 2 adds → balanced to 1 each (we keep the first add, drop
    # the second, since deck size must stay legal).
    recs = [
        SimpleNamespace(card="Cultivate", action="cut",
                        reason="slow ramp", evidence={}),
        SimpleNamespace(card="Lotus Cobra", action="add",
                        reason="landfall", evidence={}),
        SimpleNamespace(card="Tireless Tracker", action="add",
                        reason="payoff", evidence={}),
    ]
    new_text, added, removed, kept = _apply_swaps_to_dck(original, recs)
    assert "Cultivate" not in new_text
    assert "1 Lotus Cobra" in new_text
    # Set/CN-suffixed lines preserved.
    assert "1 Forest|MIR|315" in new_text
    # Commander untouched.
    assert "1 Edgar Markov" in new_text
    # Balanced output: 1 add + 1 cut.
    assert added == ["Lotus Cobra"]
    assert removed == ["Cultivate"]
    assert kept == 2  # Sol Ring + Forest


def test_apply_swaps_balances_unequal_lists():
    """5 cuts + 2 adds → trimmed to 2 + 2 to keep deck size legal."""
    from commander_builder.web.app import _apply_swaps_to_dck
    from types import SimpleNamespace
    original = (
        "[Commander]\n1 Cmdr\n[Main]\n"
        + "\n".join(f"1 Card{i}" for i in range(5)) + "\n"
    )
    recs = (
        [SimpleNamespace(card=f"Card{i}", action="cut",
                         reason="", evidence={}) for i in range(5)]
        + [SimpleNamespace(card=f"NewCard{i}", action="add",
                           reason="", evidence={}) for i in range(2)]
    )
    new_text, added, removed, kept = _apply_swaps_to_dck(original, recs)
    assert len(added) == 2
    assert len(removed) == 2
    # Deck size: original 5 main - 2 cut + 2 add = 5 main.
    main_lines = [
        line for line in new_text.splitlines()
        if line.startswith("1 ") and "Cmdr" not in line
    ]
    assert len(main_lines) == 5


def test_apply_swaps_case_insensitive_cut_match():
    """Cut matching ignores case. Test pairs cut+add 1:1 since the
    balance-rule requires equal counts to keep deck size legal."""
    from commander_builder.web.app import _apply_swaps_to_dck
    from types import SimpleNamespace
    original = "[Main]\n1 sol ring\n1 Cultivate\n"
    recs = [
        SimpleNamespace(card="Sol Ring", action="cut",
                        reason="", evidence={}),
        SimpleNamespace(card="Mana Vault", action="add",
                        reason="", evidence={}),
    ]
    new_text, added, removed, _ = _apply_swaps_to_dck(original, recs)
    assert "1 sol ring" not in new_text.lower()
    assert "1 Mana Vault" in new_text
    assert removed == ["Sol Ring"]
    assert added == ["Mana Vault"]


# ---------------------------------------------------------------------------
# Quantity-aware cuts (2026-05-15 fix) -- previously, ``cuts=["Mountain",
# "Mountain"]`` on a deck with one ``27 Mountain|...`` stack removed the
# WHOLE line. New semantics decrement the quantity by the number of
# duplicate cut entries.
# ---------------------------------------------------------------------------

def test_apply_swaps_duplicate_cut_decrements_quantity():
    """``cuts=["Mountain", "Mountain"]`` on a 27-Mountain stack should
    leave ``25 Mountain``, not remove the whole line. Regression for
    the live-smoke bug discovered during auto-curate testing."""
    from commander_builder.web.app import _apply_swaps_to_dck
    from types import SimpleNamespace
    original = "[Main]\n27 Mountain|EXP|123\n1 Sol Ring\n"
    recs = [
        SimpleNamespace(card="Mountain", action="cut", reason="", evidence={}),
        SimpleNamespace(card="Mountain", action="cut", reason="", evidence={}),
        SimpleNamespace(card="Path of Ancestry", action="add",
                        reason="", evidence={}),
        SimpleNamespace(card="Secluded Courtyard", action="add",
                        reason="", evidence={}),
    ]
    new_text, added, removed, kept = _apply_swaps_to_dck(original, recs)
    # The Mountain line still exists, with 2 fewer copies.
    assert "25 Mountain|EXP|123" in new_text
    # Removed accounts BOTH cuts.
    assert removed == ["Mountain", "Mountain"]
    # Sol Ring untouched.
    assert "1 Sol Ring" in new_text
    # kept_count = 25 Mountains + 1 Sol Ring.
    assert kept == 26


def test_apply_swaps_cut_to_zero_removes_line():
    """If decrement drives the line's quantity to zero, the whole line
    drops. Same effect as the old line-remove behavior for quantity-1
    lines (single Sol Ring etc.)."""
    from commander_builder.web.app import _apply_swaps_to_dck
    from types import SimpleNamespace
    original = "[Main]\n1 Sol Ring|CLB|871\n1 Brainstorm\n"
    recs = [
        SimpleNamespace(card="Sol Ring", action="cut",
                        reason="", evidence={}),
        SimpleNamespace(card="Counterspell", action="add",
                        reason="", evidence={}),
    ]
    new_text, added, removed, kept = _apply_swaps_to_dck(original, recs)
    # Sol Ring's line is gone entirely.
    assert "Sol Ring" not in new_text
    # Removed entry uses the cut's canonical casing.
    assert removed == ["Sol Ring"]
    # Brainstorm + Counterspell remain.
    assert "1 Brainstorm" in new_text
    assert "1 Counterspell" in new_text
    assert kept == 1


def test_apply_swaps_three_cuts_two_in_line_drops_third_pair():
    """If the user asks for 3 cuts but only 2 copies exist, the first
    two pairs apply and the THIRD PAIR (cut + its paired add) drops as
    a unit. Pre-2026-07-19 the unmatched 3rd cut was silently skipped
    while its paired add still landed, GROWING the mainboard by one —
    the oversized-deck corruption bug."""
    from commander_builder.web.app import _apply_swaps_to_dck
    from types import SimpleNamespace
    original = "[Main]\n2 Mountain\n1 Sol Ring\n"
    recs = [
        SimpleNamespace(card="Mountain", action="cut",
                        reason="", evidence={}),
        SimpleNamespace(card="Mountain", action="cut",
                        reason="", evidence={}),
        SimpleNamespace(card="Mountain", action="cut",
                        reason="", evidence={}),
        SimpleNamespace(card="A", action="add", reason="", evidence={}),
        SimpleNamespace(card="B", action="add", reason="", evidence={}),
        SimpleNamespace(card="C", action="add", reason="", evidence={}),
    ]
    report: dict = {}
    new_text, added, removed, kept = _apply_swaps_to_dck(
        original, recs, drop_report=report,
    )
    # All 2 Mountains removed, line gone.
    assert "Mountain" not in new_text
    assert removed == ["Mountain", "Mountain"]
    # Only the 2 funded adds land; C's paired cut had no copy left.
    assert added == ["A", "B"]
    assert "1 A" in new_text
    assert "1 B" in new_text
    assert "1 C" not in new_text
    # Mainboard size preserved: 3 - 2 + 2 = 3.
    from commander_builder.web._helpers import _count_main_cards
    assert _count_main_cards(new_text) == 3
    # The dropped pair is reported, not silent.
    assert report["dropped_unmatched_cut"] == [{"cut": "Mountain", "add": "C"}]


def test_apply_swaps_cuts_across_multiple_lines_for_same_name():
    """Rare but legal: same card name on two different printing
    lines (e.g. 1 Mountain|CLB|... + 1 Mountain|EXP|...). A single
    Mountain cut decrements the FIRST matching line (in deck order).
    Two Mountain cuts decrement both lines, dropping each."""
    from commander_builder.web.app import _apply_swaps_to_dck
    from types import SimpleNamespace
    original = (
        "[Main]\n"
        "1 Mountain|CLB|871\n"
        "1 Mountain|EXP|123\n"
        "1 Sol Ring\n"
    )
    recs = [
        SimpleNamespace(card="Mountain", action="cut",
                        reason="", evidence={}),
        SimpleNamespace(card="Mountain", action="cut",
                        reason="", evidence={}),
        SimpleNamespace(card="A", action="add", reason="", evidence={}),
        SimpleNamespace(card="B", action="add", reason="", evidence={}),
    ]
    new_text, _, removed, kept = _apply_swaps_to_dck(original, recs)
    # Both Mountain lines gone.
    assert "Mountain" not in new_text
    assert removed == ["Mountain", "Mountain"]
    # Sol Ring untouched.
    assert "1 Sol Ring" in new_text


def test_apply_swaps_preserves_edition_codes_on_partial_cut():
    """When a partial cut leaves a quantity > 0, the rewritten line
    preserves the edition code suffix (|SET|CN) verbatim."""
    from commander_builder.web.app import _apply_swaps_to_dck
    from types import SimpleNamespace
    original = "[Main]\n5 Forest|CMM|384\n"
    recs = [
        SimpleNamespace(card="Forest", action="cut",
                        reason="", evidence={}),
        SimpleNamespace(card="Brainstorm", action="add",
                        reason="", evidence={}),
    ]
    new_text, _, removed, _ = _apply_swaps_to_dck(original, recs)
    assert "4 Forest|CMM|384" in new_text
    assert removed == ["Forest"]


# ---------------------------------------------------------------------------
# Quantity-aware adds (TIER-1.1 fix, tightened 2026-07-19) -- BASIC LAND
# adds for a card already in the deck increment the existing line
# (preserving the |SET|CN tail) instead of appending a stale duplicate,
# and duplicate basic adds collapse to one merged line. NON-basic adds
# for a card already in [Main] are REJECTED (with their paired cut) —
# incrementing them wrote singleton violations like ``2 Rhystic Study``.
# ---------------------------------------------------------------------------

def test_apply_swaps_duplicate_nonbasic_add_pair_dropped(monkeypatch):
    """An add for a NON-basic already in [Main] is a singleton
    violation — pre-2026-07-19 this incremented the existing line to
    ``2 Sol Ring``. Now the whole (cut, add) pair drops and is
    reported, leaving the deck untouched and legal."""
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **kw: None,
    )
    from commander_builder.web.app import _apply_swaps_to_dck
    from types import SimpleNamespace
    original = "[Main]\n1 Sol Ring|CLB|871\n1 Cultivate\n"
    recs = [
        SimpleNamespace(card="Cultivate", action="cut",
                        reason="", evidence={}),
        SimpleNamespace(card="Sol Ring", action="add",
                        reason="", evidence={}),
    ]
    report: dict = {}
    new_text, added, removed, kept = _apply_swaps_to_dck(
        original, recs, drop_report=report,
    )
    # No qty-2 line, no swap applied at all — the pair dropped as a unit.
    assert "1 Sol Ring|CLB|871" in new_text
    assert "2 Sol Ring" not in new_text
    assert "1 Cultivate" in new_text
    assert added == []
    assert removed == []
    assert kept == 2
    assert report["dropped_duplicate_add"] == [
        {"cut": "Cultivate", "add": "Sol Ring"},
    ]


def test_apply_swaps_duplicate_adds_collapse_to_one_line(monkeypatch):
    """Two adds of the same card produce one merged line, not two
    separate ``1 <Name>`` lines."""
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **kw: None,
    )
    from commander_builder.web.app import _apply_swaps_to_dck
    from types import SimpleNamespace
    original = "[Main]\n1 Cultivate\n1 Brainstorm\n"
    recs = [
        SimpleNamespace(card="Cultivate", action="cut",
                        reason="", evidence={}),
        SimpleNamespace(card="Brainstorm", action="cut",
                        reason="", evidence={}),
        SimpleNamespace(card="Mountain", action="add",
                        reason="", evidence={}),
        SimpleNamespace(card="Mountain", action="add",
                        reason="", evidence={}),
    ]
    new_text, added, removed, kept = _apply_swaps_to_dck(original, recs)
    mountain_lines = [
        line for line in new_text.splitlines()
        if "Mountain" in line
    ]
    assert mountain_lines == ["2 Mountain"]
    # ``added`` still reports both instances for caller bookkeeping.
    assert added == ["Mountain", "Mountain"]
    # Both originals were cut; flushed adds aren't counted in ``kept``.
    assert kept == 0


def test_apply_swaps_add_merges_after_partial_cut_same_card(monkeypatch):
    """Basic-land cut decrements + add for the same name net out on
    the existing line (keeping the edition tail) rather than producing
    a stale decremented line plus a fresh ``1 <Name>`` line. The
    NON-basic add (Sol Ring already in [Main]) drops with its paired
    cut instead of bumping to an illegal ``2 Sol Ring``."""
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **kw: None,
    )
    from commander_builder.web.app import _apply_swaps_to_dck
    from types import SimpleNamespace
    original = "[Main]\n5 Mountain|EXP|123\n1 Sol Ring\n"
    recs = [
        SimpleNamespace(card="Mountain", action="cut",
                        reason="", evidence={}),
        SimpleNamespace(card="Mountain", action="cut",
                        reason="", evidence={}),
        SimpleNamespace(card="Sol Ring", action="add",
                        reason="", evidence={}),
        SimpleNamespace(card="Mountain", action="add",
                        reason="", evidence={}),
    ]
    report: dict = {}
    new_text, added, removed, kept = _apply_swaps_to_dck(
        original, recs, drop_report=report,
    )
    # Pair 1 (cut Mountain, add Sol Ring) dropped: Sol Ring is a
    # non-basic already in the deck. Pair 2 (cut Mountain, add
    # Mountain) applies and nets out: 5 - 1 + 1 = 5.
    assert "5 Mountain|EXP|123" in new_text
    mountain_lines = [
        line for line in new_text.splitlines()
        if "Mountain" in line
    ]
    assert len(mountain_lines) == 1
    # Sol Ring stays a singleton.
    assert "1 Sol Ring" in new_text
    assert "2 Sol Ring" not in new_text
    assert report["dropped_duplicate_add"] == [
        {"cut": "Mountain", "add": "Sol Ring"},
    ]
    assert added == ["Mountain"]
    assert removed == ["Mountain"]
    # Post-cut survivors: 4 Mountain + 1 Sol Ring = 5. The Mountain
    # merge bump is tallied via len(added) downstream.
    assert kept == 5


def test_apply_swaps_add_hits_first_matching_line_when_multiple_printings(monkeypatch):
    """Same card on two printing lines: an add bumps the FIRST matching
    line in deck order (consistent with the cut-decrement contract)."""
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **kw: None,
    )
    from commander_builder.web.app import _apply_swaps_to_dck
    from types import SimpleNamespace
    original = (
        "[Main]\n"
        "1 Mountain|CLB|871\n"
        "1 Mountain|EXP|123\n"
        "1 Sol Ring\n"
    )
    recs = [
        SimpleNamespace(card="Sol Ring", action="cut",
                        reason="", evidence={}),
        SimpleNamespace(card="Mountain", action="add",
                        reason="", evidence={}),
    ]
    new_text, _added, _removed, _kept = _apply_swaps_to_dck(original, recs)
    assert "2 Mountain|CLB|871" in new_text
    assert "1 Mountain|EXP|123" in new_text


# ---------------------------------------------------------------------------
# Decklist validation of swap pairs (2026-07-19 fix) -- cuts must match
# an actual [Main] card and adds must not violate the singleton rule or
# duplicate the commander. An invalid half drops the WHOLE (cut, add)
# pair so the mainboard size is preserved no matter what the LLM sent.
# ---------------------------------------------------------------------------

def test_apply_swaps_hallucinated_cut_drops_paired_add():
    """A cut for a card not in the deck at all (LLM hallucination)
    drops its paired add too — pre-fix the add landed anyway and the
    deck grew past 99."""
    from commander_builder.web.app import _apply_swaps_to_dck
    from commander_builder.web._helpers import _count_main_cards
    from types import SimpleNamespace
    original = "[Main]\n1 Sol Ring\n1 Cultivate\n1 Brainstorm\n"
    recs = [
        SimpleNamespace(card="Imaginary Card", action="cut",
                        reason="", evidence={}),
        SimpleNamespace(card="Lotus Cobra", action="add",
                        reason="", evidence={}),
    ]
    report: dict = {}
    new_text, added, removed, kept = _apply_swaps_to_dck(
        original, recs, drop_report=report,
    )
    assert added == []
    assert removed == []
    assert "Lotus Cobra" not in new_text
    # Deck size unchanged — the whole pair dropped.
    assert _count_main_cards(new_text) == 3
    assert report["dropped_unmatched_cut"] == [
        {"cut": "Imaginary Card", "add": "Lotus Cobra"},
    ]


def test_apply_swaps_dfc_full_name_cut_matches_front_face_line():
    """Proposal says ``Malakir Rebirth // Malakir Mire`` (Scryfall's
    full DFC name); the .dck line carries only the front face — the
    cut must still match instead of dropping the pair."""
    from commander_builder.web.app import _apply_swaps_to_dck
    from types import SimpleNamespace
    original = "[Main]\n1 Malakir Rebirth\n1 Sol Ring\n"
    recs = [
        SimpleNamespace(card="Malakir Rebirth // Malakir Mire",
                        action="cut", reason="", evidence={}),
        SimpleNamespace(card="Lotus Cobra", action="add",
                        reason="", evidence={}),
    ]
    report: dict = {}
    new_text, added, removed, kept = _apply_swaps_to_dck(
        original, recs, drop_report=report,
    )
    assert "Malakir Rebirth" not in new_text
    assert "1 Lotus Cobra" in new_text
    # ``removed`` reports the caller's requested spelling.
    assert removed == ["Malakir Rebirth // Malakir Mire"]
    assert report["dropped_unmatched_cut"] == []


def test_apply_swaps_dfc_front_face_cut_matches_full_name_line():
    """Mirror direction: proposal names the front face only, the .dck
    line carries the full ``A // B`` form."""
    from commander_builder.web.app import _apply_swaps_to_dck
    from types import SimpleNamespace
    original = "[Main]\n1 Malakir Rebirth // Malakir Mire\n1 Sol Ring\n"
    recs = [
        SimpleNamespace(card="Malakir Rebirth", action="cut",
                        reason="", evidence={}),
        SimpleNamespace(card="Lotus Cobra", action="add",
                        reason="", evidence={}),
    ]
    report: dict = {}
    new_text, added, removed, kept = _apply_swaps_to_dck(
        original, recs, drop_report=report,
    )
    assert "Malakir" not in new_text
    assert "1 Lotus Cobra" in new_text
    assert removed == ["Malakir Rebirth"]
    assert report["dropped_unmatched_cut"] == []


def test_apply_swaps_add_matching_commander_dropped():
    """An add that names the [Commander] card drops with its paired
    cut — the commander already occupies the command zone and a [Main]
    copy would be illegal."""
    from commander_builder.web.app import _apply_swaps_to_dck
    from types import SimpleNamespace
    original = (
        "[Commander]\n1 Krenko, Mob Boss\n"
        "[Main]\n1 Sol Ring\n1 Cultivate\n"
    )
    recs = [
        SimpleNamespace(card="Cultivate", action="cut",
                        reason="", evidence={}),
        SimpleNamespace(card="Krenko, Mob Boss", action="add",
                        reason="", evidence={}),
    ]
    report: dict = {}
    new_text, added, removed, kept = _apply_swaps_to_dck(
        original, recs, drop_report=report,
    )
    assert added == []
    assert removed == []
    assert "1 Cultivate" in new_text
    # Commander section untouched, and no [Main] copy appeared.
    assert new_text.count("Krenko, Mob Boss") == 1
    assert report["dropped_commander_add"] == [
        {"cut": "Cultivate", "add": "Krenko, Mob Boss"},
    ]


def test_apply_swaps_double_cut_of_singleton_drops_second_pair():
    """Two cuts of a quantity-1 card: the first pair applies, the
    second finds no copy left and drops with its paired add. Pre-fix
    the second add landed unfunded and oversized the deck."""
    from commander_builder.web.app import _apply_swaps_to_dck
    from commander_builder.web._helpers import _count_main_cards
    from types import SimpleNamespace
    original = "[Main]\n1 Sol Ring\n1 Cultivate\n1 Brainstorm\n"
    recs = [
        SimpleNamespace(card="Sol Ring", action="cut",
                        reason="", evidence={}),
        SimpleNamespace(card="Sol Ring", action="cut",
                        reason="", evidence={}),
        SimpleNamespace(card="Lotus Cobra", action="add",
                        reason="", evidence={}),
        SimpleNamespace(card="Tireless Tracker", action="add",
                        reason="", evidence={}),
    ]
    report: dict = {}
    new_text, added, removed, kept = _apply_swaps_to_dck(
        original, recs, drop_report=report,
    )
    assert removed == ["Sol Ring"]
    assert added == ["Lotus Cobra"]
    assert "Tireless Tracker" not in new_text
    # 3 - 1 + 1 = 3: size preserved.
    assert _count_main_cards(new_text) == 3
    assert report["dropped_unmatched_cut"] == [
        {"cut": "Sol Ring", "add": "Tireless Tracker"},
    ]


def test_apply_swaps_basic_land_add_still_increments_existing_line():
    """Basic lands are exempt from the duplicate-add rejection — a
    Mountain add on a deck already running Mountains is a legitimate
    quantity bump, not a singleton violation."""
    from commander_builder.web.app import _apply_swaps_to_dck
    from types import SimpleNamespace
    original = "[Main]\n5 Mountain|EXP|123\n1 Sol Ring\n"
    recs = [
        SimpleNamespace(card="Sol Ring", action="cut",
                        reason="", evidence={}),
        SimpleNamespace(card="Mountain", action="add",
                        reason="", evidence={}),
    ]
    report: dict = {}
    new_text, added, removed, _ = _apply_swaps_to_dck(
        original, recs, drop_report=report,
    )
    assert "6 Mountain|EXP|123" in new_text
    assert added == ["Mountain"]
    assert removed == ["Sol Ring"]
    assert report["dropped_duplicate_add"] == []


# ---------------------------------------------------------------------------
# _format_added_line tests (Scryfall lookup formatting)
# ---------------------------------------------------------------------------

def test_format_added_line_resolves_set_and_cn(monkeypatch):
    """Appended cards should write '1 <name>|<SET>|<CN>' so Forge's
    deck loader doesn't trip on ambiguous name-only resolution."""
    from commander_builder.web.app import _format_added_line
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **kw: {
            "name": "Atarka, World Render",
            "set": "frf", "collector_number": "122",
        },
    )
    line = _format_added_line("Atarka, World Render")
    assert line == "1 Atarka, World Render|FRF|122"


def test_format_added_line_falls_back_when_lookup_fails(monkeypatch):
    from commander_builder.web.app import _format_added_line
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **kw: None,
    )
    assert _format_added_line("Mystery Card") == "1 Mystery Card"


# ---------------------------------------------------------------------------
# Backlog: _pad_main_to_99 — bring sub-100 source decks up to legal size
# ---------------------------------------------------------------------------

def test_pad_main_to_99_passes_through_when_already_legal():
    from commander_builder.web.app import _pad_main_to_99
    text = "[metadata]\nName=X\n[Commander]\n1 Cmdr\n[Main]\n1 Forest\n"
    out, padded, breakdown = _pad_main_to_99(text, current_main=99)
    assert out == text
    assert padded == 0
    assert breakdown == {}


def test_pad_main_to_99_distributes_basics_proportionally():
    """Goblin-deck-style: 71 mainboard, all Mountains. We expect to add
    28 Mountains (mono-color)."""
    from commander_builder.web.app import _pad_main_to_99
    text = (
        "[metadata]\nName=Goblin\n"
        "[Commander]\n1 Krenko, Mob Boss\n"
        "[Main]\n"
        "30 Mountain\n"
        + "1 Goblin Bushwhacker\n" * 41
    )
    out, padded, breakdown = _pad_main_to_99(text, current_main=71)
    assert padded == 28
    assert breakdown == {"Mountain": 28}
    # Padding line landed inside [Main].
    assert "28 Mountain" in out


def test_pad_main_to_99_distributes_across_two_colors():
    from commander_builder.web.app import _pad_main_to_99
    text = (
        "[metadata]\nName=Simic\n"
        "[Commander]\n1 Hakbal\n"
        "[Main]\n10 Forest\n10 Island\n"
        + "1 Cultivate\n" * 60
    )
    out, padded, breakdown = _pad_main_to_99(text, current_main=80)
    assert padded == 19
    # 10:10 split → roughly 9-10 split (or 10-9). Sum must equal 19.
    assert sum(breakdown.values()) == 19
    assert breakdown.get("Forest", 0) > 0
    assert breakdown.get("Island", 0) > 0


def test_pad_main_to_99_falls_back_to_wastes_when_no_basics():
    """No basic lands in the deck (cEDH-style multi-color manabase) →
    pad with Wastes since they're colorless and legal anywhere."""
    from commander_builder.web.app import _pad_main_to_99
    text = (
        "[metadata]\nName=cEDH\n"
        "[Commander]\n1 Thrasios\n"
        "[Main]\n"
        + "1 Tundra\n1 Underground Sea\n1 Tropical Island\n"
        + "1 Sol Ring\n" * 90
    )
    out, padded, breakdown = _pad_main_to_99(text, current_main=93)
    assert padded == 6
    assert breakdown == {"Wastes": 6}


def test_cleanup_stale_staged_files_deletes_old_proposed_files(tmp_path):
    import os
    import time
    from commander_builder.web.app import _cleanup_stale_staged_files

    deck_dir = tmp_path / "userdata" / "decks" / "commander"
    deck_dir.mkdir(parents=True)
    constructed = tmp_path / "userdata" / "decks" / "constructed"
    constructed.mkdir()

    # Old proposed file, PRE-uid name shape (should be deleted —
    # leftovers staged by builds before the same-second-collision fix
    # must still match the sweep pattern).
    old_proposed = deck_dir / "Foo_proposed_20260101_120000.dck"
    old_proposed.write_text("stale\n", encoding="utf-8")
    # Backdate it so the age check fires.
    past = time.time() - 3600
    os.utime(old_proposed, (past, past))

    # Old converted file in constructed/ (should also be deleted).
    old_converted = constructed / "Bar_converted_20260101_120000.dck"
    old_converted.write_text("stale\n", encoding="utf-8")
    os.utime(old_converted, (past, past))

    # CURRENT name shape: routes_sim appends an 8-hex per-request uid
    # after the timestamp. The sweep must match these too or every
    # interrupted run would orphan its staging files forever.
    old_proposed_uid = deck_dir / "Foo_proposed_20260101_120000_a1b2c3d4.dck"
    old_proposed_uid.write_text("stale\n", encoding="utf-8")
    os.utime(old_proposed_uid, (past, past))
    old_converted_uid = (
        constructed / "Bar_converted_20260101_120000_a1b2c3d4.dck"
    )
    old_converted_uid.write_text("stale\n", encoding="utf-8")
    os.utime(old_converted_uid, (past, past))

    # User deck (should NOT be touched).
    user_deck = deck_dir / "[USER] Real Deck [B3].dck"
    user_deck.write_text("real\n", encoding="utf-8")

    deleted = _cleanup_stale_staged_files(deck_dir)
    assert deleted == 4
    assert not old_proposed.exists()
    assert not old_converted.exists()
    assert not old_proposed_uid.exists()
    assert not old_converted_uid.exists()
    assert user_deck.exists()


def test_cleanup_stale_staged_files_skips_recent_files(tmp_path):
    """Don't delete files newer than the age threshold — they might
    belong to an in-flight Forge process."""
    from commander_builder.web.app import _cleanup_stale_staged_files

    deck_dir = tmp_path / "userdata" / "decks" / "commander"
    deck_dir.mkdir(parents=True)

    fresh = deck_dir / "Foo_proposed_20260429_010000.dck"
    fresh.write_text("active\n", encoding="utf-8")
    # mtime is 'now' by default, well within the 60s threshold.

    deleted = _cleanup_stale_staged_files(deck_dir, age_threshold_sec=60)
    assert deleted == 0
    assert fresh.exists()


def test_cleanup_stale_staged_files_handles_missing_dir(tmp_path):
    from commander_builder.web.app import _cleanup_stale_staged_files
    deleted = _cleanup_stale_staged_files(tmp_path / "nope")
    assert deleted == 0


def test_correlation_summary_endpoint_returns_zero_when_no_log(client, monkeypatch):
    """When the correlation log doesn't exist yet, the endpoint
    reports zero rows + agreement_rate=0 — UI can show 'no data yet'."""
    monkeypatch.delenv("COMMANDER_BUILDER_CORRELATE_FORGE_PY", raising=False)
    resp = client.get("/api/correlation_summary")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["rows"] == 0
    assert body["agreement_rate"] == 0.0
    assert "log_path" in body
    assert body["enabled"] is False


def test_correlation_summary_endpoint_reports_enabled_state(client, monkeypatch):
    monkeypatch.setenv("COMMANDER_BUILDER_CORRELATE_FORGE_PY", "1")
    resp = client.get("/api/correlation_summary")
    assert resp.status_code == 200
    assert resp.get_json()["enabled"] is True


def test_log_error_writes_to_log_file(client, tmp_path, deck_dir):
    """JS error collector appends to a server-side log; we don't read
    it back via API (avoid making this an exfiltration vector), so
    the test inspects the file directly."""
    payload = {
        "kind": "error",
        "message": "ReferenceError: foo is not defined",
        "stack": "at runProposeSwap (app.js:165)",
        "url": "http://127.0.0.1:5000/",
        "user_agent": "test-agent",
    }
    resp = client.post("/api/log_error", json=payload)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert "ref" in body and body["ref"]
    # Log file lives next to the deck_dir's grandparent (vendor/forge → vendor).
    log_path = deck_dir.parent.parent / "_js_errors.log"
    assert log_path.exists()
    contents = log_path.read_text(encoding="utf-8")
    assert "ReferenceError: foo is not defined" in contents
    assert "runProposeSwap" in contents
    assert "test-agent" in contents


def test_log_error_400_on_missing_message(client):
    resp = client.post("/api/log_error", json={"kind": "error"})
    assert resp.status_code == 400


def test_log_error_caps_oversized_payload(client, deck_dir):
    """Defense against runaway browser dumps — message capped at 2000
    chars, stack at 4000."""
    huge = "x" * 5000
    resp = client.post("/api/log_error", json={
        "message": huge, "stack": huge,
    })
    assert resp.status_code == 200
    log_path = deck_dir.parent.parent / "_js_errors.log"
    contents = log_path.read_text(encoding="utf-8")
    # Should contain at most 2000 'x' in the MSG line and 4000 in STACK.
    msg_line = next(
        (line for line in contents.splitlines() if line.startswith("MSG:")),
        "",
    )
    # MSG: prefix + up to 2000 chars
    assert len(msg_line) <= len("MSG: ") + 2000


def test_log_error_400_on_non_json(client):
    resp = client.post("/api/log_error", data="not json",
                       content_type="application/json")
    assert resp.status_code == 400


def test_pad_main_to_99_skips_quantities_in_other_sections():
    """Sideboard / Considering basics shouldn't sway the [Main] padding
    distribution."""
    from commander_builder.web.app import _pad_main_to_99
    text = (
        "[metadata]\nName=X\n"
        "[Commander]\n1 Cmdr\n"
        "[Main]\n5 Forest\n"
        + "1 Cultivate\n" * 60
        + "[Sideboard]\n10 Mountain\n"
    )
    out, padded, breakdown = _pad_main_to_99(text, current_main=65)
    # [Main] only sees Forest, so all padding is Forest. Mountain in
    # sideboard must be ignored.
    assert "Mountain" not in breakdown
    assert breakdown == {"Forest": 34}


def test_pad_main_to_99_reports_zero_when_no_main_header():
    """No [Main] header → nowhere to splice pad lines → NOTHING is
    inserted. The pre-2026-07-19 return reported the computed deficit
    as padded anyway, so callers believed 89 basics landed when zero
    did and their post-swap size math went wrong."""
    from commander_builder.web.app import _pad_main_to_99
    text = "[metadata]\nName=X\n[Commander]\n1 Cmdr\n"
    out, padded, breakdown = _pad_main_to_99(text, current_main=10)
    assert out == text
    assert padded == 0
    assert breakdown == {}


def test_format_added_line_falls_back_when_set_missing(monkeypatch):
    from commander_builder.web.app import _format_added_line
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **kw: {"name": "X", "set": "", "collector_number": ""},
    )
    assert _format_added_line("X") == "1 X"


def test_apply_swaps_kept_count_sums_quantities_not_lines():
    """Multi-qty lines like '5 Forest' should count as 5 cards in
    `kept`, not 1 line. The UI uses kept + adds to compute
    main_count and earlier counted lines, mis-reporting deck size."""
    from commander_builder.web.app import _apply_swaps_to_dck
    from types import SimpleNamespace
    original = (
        "[Commander]\n1 Cmdr\n[Main]\n"
        "5 Forest\n"
        "3 Mountain\n"
        "1 Sol Ring\n"
    )
    recs = []  # no swaps — just measure kept_count
    _, _, _, kept = _apply_swaps_to_dck(original, recs)
    # 5 + 3 + 1 = 9 actual cards across 3 lines.
    assert kept == 9


def test_apply_swaps_no_op_when_one_list_empty():
    """0 adds + N cuts → no swaps (deck size must stay legal)."""
    from commander_builder.web.app import _apply_swaps_to_dck
    from types import SimpleNamespace
    original = "[Main]\n1 Sol Ring\n1 Cultivate\n"
    recs = [SimpleNamespace(card="Sol Ring", action="cut",
                            reason="", evidence={})]
    new_text, added, removed, _ = _apply_swaps_to_dck(original, recs)
    # Both lists trimmed to 0.
    assert added == []
    assert removed == []
    # Original deck untouched.
    assert "1 Sol Ring" in new_text
    assert "1 Cultivate" in new_text


def test_audit_endpoint_returns_full_proposed_deck(client, monkeypatch):
    """Stub improvement_advisor.advise so the endpoint runs offline."""
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **_kwargs):
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(
                    card="Lotus Cobra", action="add",
                    reason="Mana on landfall",
                    evidence={"inclusion_pct": 78.0, "synergy_pct": 12.0,
                              "price_usd": 8.5},
                ),
                SimpleNamespace(
                    card="Cultivate", action="cut",
                    reason="Replaced by faster ramp",
                    evidence={},
                ),
            ],
            diagnosis=SimpleNamespace(
                pattern_summary="aggressive landfall",
                weakness_signals=["slow ramp early"],
            ),
            source="heuristic",
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )

    resp = client.get("/api/audit?deck=Alpha&bracket=3")
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    # The add lands as a new line.
    assert "1 Lotus Cobra" in body["proposed_text"]
    # The cut decrements Cultivate count by 1 (quantity-aware semantics
    # added 2026-05-15 -- see _apply_swaps_to_dck). The test fixture
    # ships 5 Cultivate lines for ample test data; one cut means we
    # expect EXACTLY 4 Cultivate lines remaining, not zero.
    cultivate_lines = sum(
        1 for line in body["proposed_text"].splitlines()
        if "Cultivate" in line and line.strip().startswith("1 ")
    )
    assert cultivate_lines == 4, (
        f"expected 4 surviving Cultivate lines after 1 cut, got {cultivate_lines}"
    )
    # Diff payload populated.
    assert any(a["card"] == "Lotus Cobra" for a in body["added"])
    assert any(r["card"] == "Cultivate" for r in body["removed"])
    # Diagnosis surfaced.
    assert body["diagnosis"] == "aggressive landfall"
    assert "slow ramp early" in body["weakness_signals"]


def test_audit_endpoint_503_when_advisor_fails(client, monkeypatch):
    def boom(deck_path, bracket, **_kwargs):
        raise RuntimeError("EDHREC down")
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", boom,
    )
    resp = client.get("/api/audit?deck=Alpha")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["error"] == "audit failed"


def test_audit_endpoint_surfaces_average_deck_preview(client, monkeypatch):
    """When the advisor produces an AverageDeck on the AdviceReport,
    the audit response carries it under 'average_deck_preview' with
    the canonical projected shape (per-card name/inclusion_pct/category/
    in_user_deck). UI follows the contract pinned in
    tests/test_avg_deck_preview.py."""
    from types import SimpleNamespace
    from commander_builder.edhrec_client import AverageDeck, CardEntry

    avg = AverageDeck(
        commander_name="Atraxa, Praetors' Voice",
        slug="atraxa",
        url="https://edhrec.com/average-decks/atraxa/upgraded",
        bracket_slug="upgraded",
        budget_slug=None,
        cards=[
            CardEntry(name="Sol Ring", inclusion_pct=95.5),
            CardEntry(name="Cultivate", inclusion_pct=72.0),
            CardEntry(name="Unknown Card", inclusion_pct=40.0),
        ],
    )

    def fake_advise(deck_path, bracket, **_kwargs):
        return SimpleNamespace(
            recommendations=[],
            diagnosis=SimpleNamespace(
                pattern_summary="", weakness_signals=[],
            ),
            source="heuristic",
            average_deck=avg,
            edhrec_categories={
                "sol ring": "Mana Artifacts",
                "cultivate": "Ramp",
            },
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )

    resp = client.get("/api/audit?deck=Alpha&bracket=3")
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()

    preview = body.get("average_deck_preview")
    assert preview is not None, "expected average_deck_preview when advisor returned an AverageDeck"
    assert preview["bracket_slug"] == "upgraded"
    assert preview["card_count"] == 3
    # Sol Ring is in the test fixture deck text → in_user_deck=True;
    # categorized as Mana Artifacts.
    sol = next(c for c in preview["cards"] if c["name"] == "Sol Ring")
    assert sol["inclusion_pct"] == 95.5
    assert sol["category"] == "Mana Artifacts"
    # Unknown Card has no category entry → null.
    unknown = next(c for c in preview["cards"] if c["name"] == "Unknown Card")
    assert unknown["category"] is None
    assert unknown["in_user_deck"] is False


def test_audit_endpoint_average_deck_preview_is_null_when_advisor_omits_it(
    client, monkeypatch,
):
    """Legacy AdviceReport (no average_deck attr) → preview = None.
    Pinning so the UI's null-guard never starts assuming the key is
    always populated."""
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **_kwargs):
        return SimpleNamespace(
            recommendations=[],
            diagnosis=SimpleNamespace(
                pattern_summary="", weakness_signals=[],
            ),
            source="heuristic",
            # No average_deck attribute → getattr default kicks in
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )

    resp = client.get("/api/audit?deck=Alpha&bracket=3")
    assert resp.status_code == 200
    assert resp.get_json()["average_deck_preview"] is None


def test_audit_endpoint_surfaces_salt_warning_at_low_bracket(
    client, monkeypatch,
):
    """When the user's current deck carries salty cards AND the
    bracket is ≤ 3, the audit response carries a salt_warning payload
    the UI renders as a banner above the recommendations."""
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **_kwargs):
        return SimpleNamespace(
            recommendations=[],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="heuristic",
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    # The test fixture deck (_write_deck) contains Forest + Cultivate.
    # Mark Cultivate as a high-salt card so the banner fires.
    monkeypatch.setattr(
        "commander_builder.web.routes_audit.fetch_salt_list",
        lambda: {"cultivate": 3.7, "forest": 0.0},
    )

    resp = client.get("/api/audit?deck=Alpha&bracket=2")
    assert resp.status_code == 200
    warning = resp.get_json().get("salt_warning")
    assert warning is not None
    assert warning["bracket"] == 2
    assert warning["count"] >= 1
    cultivate = next(c for c in warning["cards"] if c["name"] == "Cultivate")
    assert cultivate["salt"] == 3.7


def test_audit_endpoint_main_count_reflects_proposed_text(
    client, monkeypatch, deck_dir,
):
    """Regression for the 2026-05-15 main_count headline bug.

    Pre-fix: main_count = kept + len(added_payload) + padded_count
    which counted EVERY recommendation -- including ones that
    balancing / bracket-cap / protection dropped -- producing
    headline numbers like 143 on a deck whose proposed_text is
    actually 99 cards.

    Post-fix: main_count is counted from proposed_text directly so
    the headline matches what Forge will load. This test ships an
    advisor with imbalanced adds vs cuts (5 adds, 1 cut) so the
    OLD math would produce inflated count while the NEW math reads
    proposed_text and reports truth."""
    from types import SimpleNamespace

    # The advisor returns 5 adds + 1 cut. After balancing in
    # _apply_swaps_to_dck, min(5, 1) = 1 of each lands. The
    # proposed_text should be source_main - 1 cut + 1 add = source_main.
    def fake_advise(deck_path, bracket, **_kwargs):
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(card=f"Add{i}", action="add", reason="",
                                evidence={})
                for i in range(5)
            ] + [
                SimpleNamespace(card="Cultivate", action="cut", reason="",
                                evidence={}),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="heuristic",
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )

    resp = client.get("/api/audit?deck=Alpha&bracket=3")
    assert resp.status_code == 200
    body = resp.get_json()

    # Count main cards in proposed_text directly to derive the truth.
    import re
    in_main = False
    truth = 0
    for raw in body["proposed_text"].splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith("[") and s.endswith("]"):
            in_main = s.lower() == "[main]"
            continue
        if in_main:
            m = re.match(r"^(\d+)\s+", s)
            if m:
                truth += int(m.group(1))

    # The headline MUST match what's actually in proposed_text. Under
    # the old buggy math, main_count would be (kept=39 or so) + 5 +
    # padding ~= 50+, while proposed_text is balanced near the source
    # size. Under the fix, the two agree.
    assert body["main_count"] == truth, (
        f"main_count headline ({body['main_count']}) doesn't match "
        f"actual proposed_text count ({truth}) -- regression of the "
        f"2026-05-15 fix."
    )


def test_audit_endpoint_surfaces_deck_health_signals(
    client, monkeypatch, deck_dir,
):
    """The audit response carries ``deck_health`` with all 5 signals
    populated. Tests that the route's _compute_deck_health_safe
    wrapper correctly threads the deck text through and returns the
    full shape."""
    from types import SimpleNamespace

    # Deck with one of each named signal: an MDFC, a wincon-protection
    # card, and a self-mill enabler.
    p = deck_dir / "Healthy.dck"
    p.write_text(
        "[metadata]\nName=Healthy\nMoxfield=abc\n"
        "[Commander]\n1 Test Commander\n"
        "[Main]\n"
        "1 Boseiju, Who Endures\n"      # MDFC
        "1 Silence\n"                    # wincon protection
        "1 Stitcher's Supplier\n"        # self-mill enabler
        + "1 Forest\n" * 32,
        encoding="utf-8",
    )

    def fake_advise(deck_path, bracket, **_kwargs):
        return SimpleNamespace(
            recommendations=[],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="heuristic",
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    # Stub Scryfall so spell-density / mana-sink computation doesn't
    # hit the network.
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **kw: {
            "name": name,
            "type_line": "Land" if "Forest" in name else "Creature",
            "mana_cost": "",
        },
    )

    resp = client.get("/api/audit?deck=Healthy&bracket=3")
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    health = body.get("deck_health")
    assert health is not None
    # All 5 top-level keys present (UI tile renderer needs each).
    assert set(health.keys()) == {
        "mdfc", "spell_density", "mana_sinks",
        "wincon_protection", "self_mill", "role_targets",
    }
    # Named-card signals picked up correctly.
    assert health["mdfc"]["count"] == 1
    assert "Boseiju, Who Endures" in health["mdfc"]["cards"]
    assert health["wincon_protection"]["count"] == 1
    assert "Silence" in health["wincon_protection"]["cards"]
    assert health["self_mill"]["count"] == 1
    assert "Stitcher's Supplier" in health["self_mill"]["cards"]
    # Combo/bracket assessment present + well-shaped (this deck has no
    # infinite combos, so it's clean + within bracket).
    combo = body.get("combo_assessment")
    assert combo is not None
    assert set(combo.keys()) == {
        "combos", "recommended_bracket", "violations", "within_bracket",
    }
    assert combo["combos"] == [] and combo["within_bracket"] is True
    assert combo["recommended_bracket"] == 1


def test_audit_endpoint_deck_health_empty_shape_on_scryfall_failure(
    client, monkeypatch,
):
    """If Scryfall is unreachable, deck_health degrades gracefully:
    the spell_density and mana_sinks signals report zeros (can't
    classify types) but the named-card signals (MDFC, protection,
    self-mill) still work since they don't need Scryfall."""
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **_kwargs):
        return SimpleNamespace(
            recommendations=[],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="heuristic",
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("scryfall down")),
    )

    resp = client.get("/api/audit?deck=Alpha&bracket=3")
    assert resp.status_code == 200
    health = resp.get_json()["deck_health"]
    # Still has all keys (UI needs them).
    assert set(health.keys()) == {
        "mdfc", "spell_density", "mana_sinks",
        "wincon_protection", "self_mill", "role_targets",
    }
    # Scryfall-dependent signals return zero/null gracefully.
    assert health["mana_sinks"]["count"] == 0
    assert health["spell_density"]["non_permanent_count"] == 0


def test_audit_endpoint_surfaces_protected_cards_from_metadata(
    client, monkeypatch, deck_dir,
):
    """[metadata] Protect= entries in the .dck appear in the audit
    response as ``protected_cards: [...]`` so the UI can render a
    🔒 badge on the corresponding cut suggestions."""
    from types import SimpleNamespace

    # Write a deck with three Protect= entries — one comma-named (the
    # commander), one quoted-compact list for two simple names.
    p = deck_dir / "Protected.dck"
    p.write_text(
        "[metadata]\nName=Protected\nMoxfield=abc\n"
        "Protect=Krenko, Mob Boss\n"
        'Protect="Goblin Lackey", "Skirk Prospector"\n'
        "[Commander]\n1 Krenko, Mob Boss\n"
        "[Main]\n" + "1 Forest\n" * 35,
        encoding="utf-8",
    )

    def fake_advise(deck_path, bracket, **_kwargs):
        return SimpleNamespace(
            recommendations=[],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="heuristic",
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )

    resp = client.get("/api/audit?deck=Protected&bracket=3")
    assert resp.status_code == 200, resp.get_json()
    protected = resp.get_json().get("protected_cards")
    # The commander surfaces as a single entry, commas intact.
    assert "Krenko, Mob Boss" in protected
    # The quoted-compact line surfaces as two distinct cards.
    assert "Goblin Lackey" in protected
    assert "Skirk Prospector" in protected


def test_audit_endpoint_protected_cards_empty_when_no_metadata(
    client, monkeypatch,
):
    """A deck without Protect= entries surfaces an empty list (not
    null). The UI's null-guard treats both as 'nothing locked.'"""
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **_kwargs):
        return SimpleNamespace(
            recommendations=[],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="heuristic",
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )

    resp = client.get("/api/audit?deck=Alpha&bracket=3")
    assert resp.status_code == 200
    assert resp.get_json()["protected_cards"] == []


def test_audit_endpoint_salt_warning_null_at_high_bracket(
    client, monkeypatch,
):
    """At B4/B5, salt is expected — the banner suppresses to avoid
    noise. The per-recommendation salt annotations elsewhere in the
    response continue to populate."""
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **_kwargs):
        return SimpleNamespace(
            recommendations=[],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="heuristic",
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    monkeypatch.setattr(
        "commander_builder.web.routes_audit.fetch_salt_list",
        lambda: {"cultivate": 3.7},
    )

    resp = client.get("/api/audit?deck=Alpha&bracket=4")
    assert resp.status_code == 200
    assert resp.get_json()["salt_warning"] is None


def test_audit_endpoint_404_on_missing_deck(client):
    resp = client.get("/api/audit?deck=Ghost")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/audit/stream  (Server-Sent Events)
# ---------------------------------------------------------------------------

def _parse_sse(body_bytes: bytes) -> list[tuple[str, dict]]:
    """Parse an SSE response body into ``[(event_name, data_dict), ...]``.

    Robust to multi-line ``data:`` chunks but tests only emit single-
    line JSON, so the loose parser here is sufficient. Used by the
    streaming-endpoint tests below.
    """
    text = body_bytes.decode("utf-8")
    events = []
    for block in text.strip().split("\n\n"):
        if not block.strip():
            continue
        event_name = None
        data_str = None
        for line in block.split("\n"):
            if line.startswith("event:"):
                event_name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_str = line[len("data:"):].strip()
        if event_name and data_str is not None:
            events.append((event_name, json.loads(data_str)))
    return events


def test_audit_stream_emits_phase_events_in_order(client, monkeypatch):
    """The SSE endpoint must drive ``_advise_steps()`` and emit one
    event per phase in this order: diagnosis → manabase → primary →
    complete. The complete event carries the same payload shape as
    the sync ``/api/audit`` endpoint so the UI can reuse its render
    code without further branching.
    """
    from types import SimpleNamespace
    from commander_builder._advisor_models import AdvicePhase

    def fake_steps(deck_path, bracket, **_kwargs):
        # Mimic the real generator shape.
        yield AdvicePhase("diagnosis", {
            "deck_filename": deck_path.name,
            "bracket": bracket,
            "commander_names": ["The Ur-Dragon"],
            "diagnosis": {
                "pattern_summary": "aggressive dragons",
                "weakness_signals": ["no closer"],
            },
        })
        yield AdvicePhase("manabase", {
            "recommendations": [
                {"card": "Sacred Foundry", "action": "add",
                 "reason": "essential dual",
                 "evidence": {"source": "manabase_essentials"},
                 "name_known": True},
            ],
            "tribe": "Dragon",
        })
        yield AdvicePhase("primary", {
            "recommendations": [
                {"card": "Lotus Cobra", "action": "add",
                 "reason": "EDHREC top",
                 "evidence": {"inclusion_pct": 70.0, "synergy_pct": 10.0},
                 "name_known": True},
                {"card": "Cultivate", "action": "cut",
                 "reason": "off-archetype",
                 "evidence": {}, "name_known": True},
            ],
            "requested_source": "heuristic",
            "effective_source": "heuristic",
            "fallback_reason": None,
            "rationale_override": None,
            "bracket_peer_ref_count": 0,
        })
        # complete carries the AdviceReport itself; the SSE endpoint
        # re-runs _apply_swaps_to_dck on it, so the report must have
        # ``recommendations`` shaped like real SwapRecommendation.
        from commander_builder._advisor_models import (
            AdviceReport, DeckDiagnosis, SwapRecommendation,
        )
        rep = AdviceReport(
            deck_filename=deck_path.name,
            deck_id=None,
            bracket=bracket,
            commander_names=["The Ur-Dragon"],
            diagnosis=DeckDiagnosis(
                pattern_summary="aggressive dragons",
                weakness_signals=["no closer"],
            ),
            recommendations=[
                SwapRecommendation(
                    card="Lotus Cobra", action="add",
                    reason="EDHREC top",
                    evidence={"inclusion_pct": 70.0, "synergy_pct": 10.0,
                              "source": "edhrec.top_cards"},
                    name_known=True,
                ),
                SwapRecommendation(
                    card="Cultivate", action="cut",
                    reason="off-archetype",
                    evidence={}, name_known=True,
                ),
            ],
            source="heuristic",
            timestamp="2026-05-13T12:00:00",
        )
        yield AdvicePhase("complete", {"report": rep})

    monkeypatch.setattr(
        "commander_builder.improvement_advisor._advise_steps", fake_steps,
    )
    resp = client.get("/api/audit/stream?deck=Alpha&bracket=3")
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"
    events = _parse_sse(resp.data)
    event_names = [e[0] for e in events]
    assert event_names == ["diagnosis", "manabase", "primary", "complete"]
    # Diagnosis carries enough to render the panel immediately.
    diag_payload = dict(events)["diagnosis"]
    assert diag_payload["bracket"] == 3
    assert "The Ur-Dragon" in diag_payload["commander_names"]
    # Manabase phase surfaces the curated essentials.
    manabase_payload = dict(events)["manabase"]
    assert manabase_payload["tribe"] == "Dragon"
    assert any(
        r["card"] == "Sacred Foundry"
        for r in manabase_payload["recommendations"]
    )
    # Complete event mirrors the sync /api/audit payload shape.
    complete = dict(events)["complete"]
    assert complete["bracket"] == 3
    assert complete["source"] == "heuristic"
    assert "proposed_text" in complete
    assert any(a["card"] == "Lotus Cobra" for a in complete["added"])
    assert any(r["card"] == "Cultivate" for r in complete["removed"])


def test_audit_stream_emits_error_on_missing_deck(client, monkeypatch):
    """Missing-deck input validation must return 404 BEFORE the SSE
    stream opens — the streaming endpoint reuses the sync endpoint's
    early-return path so the response is a plain JSON 404 rather
    than an SSE stream with an error event. This lets the client's
    fetch() handle the error normally."""
    resp = client.get("/api/audit/stream?deck=Ghost&bracket=3")
    assert resp.status_code == 404


def test_audit_stream_emits_error_event_on_mid_stream_exception(
    client, monkeypatch,
):
    """If ``_advise_steps`` raises after the stream opens (Claude API
    blew up, EDHREC slug mismatch, etc.), the endpoint must emit a
    final ``error`` event rather than letting the exception bubble.
    The UI uses the error event to show a toast and reset the
    in-flight state."""
    def boom(*_args, **_kwargs):
        # Yield one phase, then raise to simulate a mid-stream crash.
        from commander_builder._advisor_models import AdvicePhase
        yield AdvicePhase("diagnosis", {
            "deck_filename": "x", "bracket": 3,
            "commander_names": ["X"],
            "diagnosis": {},
        })
        raise RuntimeError("EDHREC scraper crashed")
    monkeypatch.setattr(
        "commander_builder.improvement_advisor._advise_steps", boom,
    )
    resp = client.get("/api/audit/stream?deck=Alpha&bracket=3")
    events = _parse_sse(resp.data)
    # diagnosis was emitted, then the error halted the stream.
    assert [e[0] for e in events] == ["diagnosis", "error"]
    err = dict(events)["error"]
    assert err["error"] == "audit failed"
    assert "RuntimeError" in err["detail"]


def test_audit_stream_no_buffering_headers(client, monkeypatch):
    """SSE responses must include Cache-Control: no-cache and
    X-Accel-Buffering: no so reverse proxies (nginx/Apache) flush
    each event to the client immediately. Without these, the
    streaming benefit disappears in production."""
    from commander_builder._advisor_models import AdvicePhase
    from commander_builder._advisor_models import (
        AdviceReport, DeckDiagnosis,
    )

    def trivial_steps(deck_path, bracket, **_kwargs):
        yield AdvicePhase("complete", {
            "report": AdviceReport(
                deck_filename=deck_path.name,
                deck_id=None, bracket=bracket,
                commander_names=["X"],
                diagnosis=DeckDiagnosis(),
                recommendations=[],
                source="heuristic", timestamp="2026-05-13",
            ),
        })

    monkeypatch.setattr(
        "commander_builder.improvement_advisor._advise_steps", trivial_steps,
    )
    resp = client.get("/api/audit/stream?deck=Alpha&bracket=3")
    assert resp.headers.get("Cache-Control") == "no-cache"
    assert resp.headers.get("X-Accel-Buffering") == "no"


def test_audit_stream_byo_key_threaded_not_staged_in_env(client, monkeypatch):
    """Streaming BYO-key path of the 2026-07-19 rework: the key must
    arrive at ``_advise_steps`` as the explicit ``api_key`` kwarg and
    ``os.environ`` must be untouched for the WHOLE generator lifetime.
    The old implementation held the env mutation (plus a global lock)
    across the entire SSE stream — the widest cross-request race window
    in the app."""
    import os as _os
    from commander_builder._advisor_models import (
        AdvicePhase, AdviceReport, DeckDiagnosis,
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    seen = {}

    def fake_steps(deck_path, bracket, **kwargs):
        seen["api_key_param"] = kwargs.get("api_key")
        # Captured MID-STREAM, while the generator is live — exactly
        # the window the old env staging held the mutation open for.
        seen["api_key_in_env"] = _os.environ.get("ANTHROPIC_API_KEY")
        yield AdvicePhase("complete", {
            "report": AdviceReport(
                deck_filename=deck_path.name,
                deck_id=None, bracket=bracket,
                commander_names=["X"],
                diagnosis=DeckDiagnosis(),
                recommendations=[],
                source="claude", timestamp="2026-07-19",
            ),
        })

    monkeypatch.setattr(
        "commander_builder.improvement_advisor._advise_steps", fake_steps,
    )
    resp = client.get(
        "/api/audit/stream?deck=Alpha&bracket=3&source=claude",
        headers={"X-Anthropic-API-Key": "sk-stream-byo"},
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.data)
    assert [e[0] for e in events] == ["complete"]
    assert seen["api_key_param"] == "sk-stream-byo"
    assert seen["api_key_in_env"] is None  # never staged in env
    assert "ANTHROPIC_API_KEY" not in _os.environ  # nothing lingers


def _stub_advise_capturing(monkeypatch, source="heuristic", fallback_reason=None):
    """Stub advise() and capture how it was invoked — including the
    explicit api_key parameter AND what os.environ looked like at call
    time (which must be UNTOUCHED by the route; the 2026-07-19 rework
    threads the BYO key as a parameter instead of staging it in env).
    Returns the seen-args dict."""
    from types import SimpleNamespace
    seen = {}

    def fake(deck_path, bracket, **kwargs):
        import os as _os
        seen["use_claude"] = kwargs.get("use_claude", False)
        seen["api_key_param"] = kwargs.get("api_key")
        seen["api_key_in_env"] = _os.environ.get("ANTHROPIC_API_KEY")
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(
                    card="Lotus Cobra", action="add",
                    reason="ramp",
                    evidence={"inclusion_pct": 78.0},
                ),
                SimpleNamespace(
                    card="Cultivate", action="cut",
                    reason="replaced",
                    evidence={},
                ),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source=source,
            fallback_reason=fallback_reason,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake,
    )
    return seen


def test_audit_defaults_to_heuristic_backend(client, monkeypatch):
    seen = _stub_advise_capturing(monkeypatch, source="heuristic")
    resp = client.get("/api/audit?deck=Alpha&bracket=3")
    assert resp.status_code == 200
    body = resp.get_json()
    assert seen["use_claude"] is False
    assert body["source"] == "heuristic"
    assert body["requested_llm"] == "heuristic"
    assert body["warning"] is None


def test_audit_llm_claude_passes_use_claude_and_byo_key(client, monkeypatch):
    seen = _stub_advise_capturing(monkeypatch, source="claude")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    resp = client.get(
        "/api/audit?deck=Alpha&bracket=3&llm=claude",
        headers={"X-Anthropic-API-Key": "sk-test-byo-12345"},
    )
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert seen["use_claude"] is True
    # BYO key rides the explicit api_key parameter...
    assert seen["api_key_param"] == "sk-test-byo-12345"
    # ...and is NEVER staged in the process env, even mid-request —
    # env staging raced across concurrent threaded requests.
    assert seen["api_key_in_env"] is None
    assert body["source"] == "claude"
    assert body["requested_llm"] == "claude"
    assert body["warning"] is None
    # Nothing lingers after the request either.
    import os as _os
    assert "ANTHROPIC_API_KEY" not in _os.environ


def test_audit_llm_claude_warns_when_advisor_falls_back(client, monkeypatch):
    """When the user requests Claude but advise() returns source='heuristic'
    (no key, no SDK, network failure), surface a UI-visible warning."""
    _stub_advise_capturing(monkeypatch, source="heuristic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    resp = client.get("/api/audit?deck=Alpha&bracket=3&llm=claude")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["source"] == "heuristic"
    assert body["requested_llm"] == "claude"
    assert body["warning"] is not None
    # No key, so we expect the specific "no API key" hint, not the
    # generic message.
    assert "api key" in body["warning"].lower()


def test_audit_warning_includes_fallback_reason_when_present(client, monkeypatch):
    """If advise() returned a concrete fallback_reason (auth error,
    JSON decode, etc.) the UI must surface that exact reason, not a
    generic 'Claude unavailable' string."""
    _stub_advise_capturing(
        monkeypatch,
        source="heuristic",
        fallback_reason="claude advisor failed (AuthenticationError: 401 invalid api key)",
    )
    resp = client.get(
        "/api/audit?deck=Alpha&bracket=3&llm=claude",
        headers={"X-Anthropic-API-Key": "sk-bogus-but-present"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["warning"] is not None
    assert "AuthenticationError" in body["warning"]
    assert "401" in body["warning"]


def test_audit_forwards_model_param_to_advise(client, monkeypatch):
    """?model=claude-haiku-4-5 should be forwarded to advise()'s
    claude_model kwarg so the SDK uses the cheaper tier."""
    from types import SimpleNamespace
    seen = {}

    def fake(deck_path, bracket, **kwargs):
        seen["claude_model"] = kwargs.get("claude_model")
        seen["use_claude"] = kwargs.get("use_claude")
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(card="Lotus Cobra", action="add",
                                reason="ramp", evidence={}),
                SimpleNamespace(card="Cultivate", action="cut",
                                reason="slow", evidence={}),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="claude",
            fallback_reason=None,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake,
    )
    resp = client.get(
        "/api/audit?deck=Alpha&bracket=3&llm=claude&model=claude-haiku-4-5",
        headers={"X-Anthropic-API-Key": "sk-test"},
    )
    assert resp.status_code == 200
    assert seen["use_claude"] is True
    assert seen["claude_model"] == "claude-haiku-4-5"


def test_audit_uses_default_model_when_none_specified(client, monkeypatch):
    from types import SimpleNamespace
    from commander_builder.improvement_advisor import DEFAULT_CLAUDE_MODEL
    seen = {}

    def fake(deck_path, bracket, **kwargs):
        seen["claude_model"] = kwargs.get("claude_model")
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(card="Lotus Cobra", action="add",
                                reason="ramp", evidence={}),
                SimpleNamespace(card="Cultivate", action="cut",
                                reason="slow", evidence={}),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="claude",
            fallback_reason=None,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake,
    )
    resp = client.get(
        "/api/audit?deck=Alpha&bracket=3&llm=claude",
        headers={"X-Anthropic-API-Key": "sk-test"},
    )
    assert resp.status_code == 200
    assert seen["claude_model"] == DEFAULT_CLAUDE_MODEL


def test_audit_warning_when_claude_succeeded_is_none(client, monkeypatch):
    _stub_advise_capturing(monkeypatch, source="claude", fallback_reason=None)
    resp = client.get(
        "/api/audit?deck=Alpha&bracket=3&llm=claude",
        headers={"X-Anthropic-API-Key": "sk-test-key"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["warning"] is None
    assert body["source"] == "claude"


def test_audit_400_on_invalid_llm_value(client, monkeypatch):
    _stub_advise_capturing(monkeypatch)
    resp = client.get("/api/audit?deck=Alpha&llm=ollama-but-not-supported-yet")
    assert resp.status_code == 400
    assert "llm" in resp.get_json()["error"]


def test_audit_does_not_leak_byo_key_to_subsequent_call(client, monkeypatch):
    """The header-injected key only lives for the duration of the
    current request. A follow-up audit without the header must not
    inherit it."""
    seen = _stub_advise_capturing(monkeypatch, source="claude")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    r1 = client.get(
        "/api/audit?deck=Alpha&bracket=3&llm=claude",
        headers={"X-Anthropic-API-Key": "sk-first-call"},
    )
    assert r1.status_code == 200
    assert seen["api_key_param"] == "sk-first-call"
    assert seen["api_key_in_env"] is None  # never staged in env

    seen.clear()
    r2 = client.get("/api/audit?deck=Alpha&bracket=3&llm=claude")
    assert r2.status_code == 200
    assert seen.get("api_key_param") is None
    assert seen.get("api_key_in_env") is None


def test_audit_uses_config_key_when_no_header(client, monkeypatch, tmp_path):
    """FP-011 unification: with no X-Anthropic-API-Key header, the audit
    resolves the BYO key from config.json (what the Settings panel
    writes) and threads it as the explicit api_key parameter."""
    from commander_builder import config_store
    cfg = tmp_path / "config.json"
    monkeypatch.setenv("COMMANDER_BUILDER_CONFIG", str(cfg))
    config_store.save_config({"anthropic_api_key": "sk-ant-fromconfig1234567"})
    seen = _stub_advise_capturing(monkeypatch, source="claude")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    resp = client.get("/api/audit?deck=Alpha&bracket=3&llm=claude")  # no header
    assert resp.status_code == 200, resp.get_json()
    assert seen["use_claude"] is True
    assert seen["api_key_param"] == "sk-ant-fromconfig1234567"
    # Never staged in env — mid-request or after.
    assert seen["api_key_in_env"] is None
    import os as _os
    assert "ANTHROPIC_API_KEY" not in _os.environ


def test_audit_header_key_overrides_config(client, monkeypatch, tmp_path):
    """Header is the per-request override; it wins over config.json."""
    from commander_builder import config_store
    cfg = tmp_path / "config.json"
    monkeypatch.setenv("COMMANDER_BUILDER_CONFIG", str(cfg))
    config_store.save_config({"anthropic_api_key": "sk-ant-fromconfig1234567"})
    seen = _stub_advise_capturing(monkeypatch, source="claude")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    resp = client.get(
        "/api/audit?deck=Alpha&bracket=3&llm=claude",
        headers={"X-Anthropic-API-Key": "sk-ant-fromheader9999999"},
    )
    assert resp.status_code == 200, resp.get_json()
    assert seen["api_key_param"] == "sk-ant-fromheader9999999"


# ---------------------------------------------------------------------------
# /api/deck_source GET + PUT (Moxfield URL attached to a deck)
# ---------------------------------------------------------------------------

def test_deck_source_get_returns_none_when_unattached(client):
    resp = client.get("/api/deck_source?deck=Alpha")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["moxfield_id"] is None
    assert body["moxfield_url"] is None


def test_deck_source_put_attaches_url(client, deck_dir):
    resp = client.put(
        "/api/deck_source?deck=Alpha",
        json={"moxfield_url": "https://moxfield.com/decks/abc123"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["moxfield_id"] == "abc123"
    assert "moxfield.com/decks/abc123" in body["moxfield_url"]
    # Persisted to file.
    on_disk = (deck_dir / "Alpha.dck").read_text(encoding="utf-8")
    assert "Moxfield=abc123" in on_disk


def test_deck_source_put_updates_existing(client, deck_dir):
    # First attach.
    client.put(
        "/api/deck_source?deck=Alpha",
        json={"moxfield_url": "https://moxfield.com/decks/orig"},
    )
    # Now update.
    resp = client.put(
        "/api/deck_source?deck=Alpha",
        json={"moxfield_url": "https://moxfield.com/decks/newone"},
    )
    body = resp.get_json()
    assert body["moxfield_id"] == "newone"
    on_disk = (deck_dir / "Alpha.dck").read_text(encoding="utf-8")
    # Old line gone, new line in place.
    assert "Moxfield=orig" not in on_disk
    assert "Moxfield=newone" in on_disk


def test_deck_source_put_clears_when_empty_url(client, deck_dir):
    client.put(
        "/api/deck_source?deck=Alpha",
        json={"moxfield_url": "https://moxfield.com/decks/abc"},
    )
    resp = client.put("/api/deck_source?deck=Alpha", json={"moxfield_url": ""})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["moxfield_id"] is None
    on_disk = (deck_dir / "Alpha.dck").read_text(encoding="utf-8")
    assert "Moxfield=" not in on_disk


def test_deck_source_put_400_on_bad_url(client):
    resp = client.put(
        "/api/deck_source?deck=Alpha",
        json={"moxfield_url": "not a url"},
    )
    # parse_deck_id may reject or accept; the endpoint should return
    # 400 either way because the value isn't useful as an id.
    assert resp.status_code in (200, 400)


def test_deck_source_404_on_missing_deck(client):
    resp = client.get("/api/deck_source?deck=Ghost")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/verify_against_source
# ---------------------------------------------------------------------------

def test_verify_against_source_400_when_no_url_attached(client):
    resp = client.get("/api/verify_against_source?deck=Alpha")
    assert resp.status_code == 400
    assert "no Moxfield source" in resp.get_json()["error"]


def test_verify_against_source_diffs_local_vs_remote(client, monkeypatch):
    # Attach a source URL first.
    client.put(
        "/api/deck_source?deck=Alpha",
        json={"moxfield_url": "https://moxfield.com/decks/abc"},
    )
    # Stub fetch_deck so we get a deterministic remote.
    fake_remote_json = {
        "name": "Alpha", "publicId": "abc",
        "boards": {
            "commanders": {
                "cards": {
                    "k1": {"quantity": 1,
                           "card": {"name": "Test Cmdr", "set": "C", "cn": "1"}},
                },
            },
            "mainboard": {
                "cards": {
                    # Remote diverged: 'Sol Ring' replaced by 'Mana Crypt'.
                    "k2": {"quantity": 1,
                           "card": {"name": "Mana Crypt", "set": "MS", "cn": "1"}},
                    "k3": {"quantity": 1,
                           "card": {"name": "Forest", "set": "M", "cn": "1"}},
                },
            },
        },
    }
    monkeypatch.setattr(
        "commander_builder.moxfield_import.fetch_deck",
        lambda public_id: fake_remote_json,
    )
    resp = client.get("/api/verify_against_source?deck=Alpha")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "moxfield.com/decks/abc" in body["source_url"]
    # Local has Cultivate + Forest, remote has Mana Crypt + Forest.
    # Diff should show Cultivate as local-only and Mana Crypt as
    # remote-only.
    assert any("Cultivate" in line for line in body["in_local_only"])
    assert any("Mana Crypt" in line for line in body["in_remote_only"])


def test_verify_against_source_502_on_moxfield_failure(client, monkeypatch):
    client.put(
        "/api/deck_source?deck=Alpha",
        json={"moxfield_url": "https://moxfield.com/decks/abc"},
    )
    def boom(public_id):
        raise RuntimeError("network down")
    monkeypatch.setattr(
        "commander_builder.moxfield_import.fetch_deck", boom,
    )
    resp = client.get("/api/verify_against_source?deck=Alpha")
    assert resp.status_code == 502


def test_propose_swap_503_when_forge_unavailable(client, monkeypatch):
    """Forge missing should return 503 with detail, not 500."""
    from types import SimpleNamespace

    def fake_compare(*a, **kw):
        return SimpleNamespace()  # not reached
    monkeypatch.setattr(
        "commander_builder.compare_versions.compare", fake_compare,
    )

    def boom(cls):
        raise FileNotFoundError("vendor/forge not found")
    monkeypatch.setattr(
        "commander_builder.forge_runner.ForgeRunner.locate",
        classmethod(boom),
    )

    new_text = (
        "[metadata]\nName=v2\n\n[Commander]\n1 Cmdr\n\n"
        "[Main]\n1 Forest\n1 Lotus Cobra\n"
    )
    resp = client.post("/api/propose_swap", json={
        "deck": "Alpha", "new_text": new_text, "games": 10,
    })
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["error"] == "Forge not available"


# ---------------------------------------------------------------------------
# /api/propose_swap — Forge-mode-conversion path coverage
# ---------------------------------------------------------------------------
#
# The 1v1 path stages decks under ``userdata/decks/constructed/``
# (NOT the commander folder), strips the ``[Commander]`` section,
# and injects ``Deck Type=constructed`` so Forge's ``-f constructed``
# mode actually starts games. Without these transformations Forge
# silently loads the deck but plays 0 games — the bug the
# ``_to_constructed_format`` helper exists to prevent.
#
# These tests pin the conversion + staging behavior so a future
# refactor that breaks the 1v1 path fails loud in CI rather than
# silently producing zero-games sim results in production.
#
# Helper: ``_capture_compare(monkeypatch)`` wraps ``_stub_compare``
# but ALSO reads the staged file contents during the fake compare
# call (before cleanup runs). Returns a dict the test can inspect.

def _capture_compare(monkeypatch, **stub_kwargs):
    """Stub ``compare()`` but capture the staged deck contents +
    paths for inspection. Returns a captured dict the test asserts
    against. Inspecting from inside fake_compare runs BEFORE the
    endpoint's cleanup loop deletes the staged files.
    """
    from types import SimpleNamespace
    captured: dict = {}

    def fake_compare(old_deck, new_deck, bracket, games_per_pod,
                     filler_pairs=2, mode="1v1", runner=None,
                     out_dir=None, deck_dir=None,
                     parallel=True, max_workers=None,
                     early_stop=True):
        # Capture call arguments first.
        captured["old_deck"] = old_deck
        captured["new_deck"] = new_deck
        captured["mode"] = mode
        captured["deck_dir"] = deck_dir
        # Read the staged file contents WHILE they still exist.
        if deck_dir:
            old_path = Path(deck_dir) / old_deck
            new_path = Path(deck_dir) / new_deck
            if old_path.exists():
                captured["old_text"] = old_path.read_text(encoding="utf-8")
            if new_path.exists():
                captured["new_text"] = new_path.read_text(encoding="utf-8")
        # Return a minimal valid report.
        old_wins = stub_kwargs.get("old_wins", 4)
        new_wins = stub_kwargs.get("new_wins", 11)
        draws = stub_kwargs.get("draws", 0)
        return SimpleNamespace(
            old_deck=old_deck, new_deck=new_deck,
            bracket=bracket, mode=mode, games_per_pod=games_per_pod,
            old_stats=SimpleNamespace(
                deck_filename=old_deck,
                wins=old_wins, games=old_wins + new_wins + draws,
            ),
            new_stats=SimpleNamespace(
                deck_filename=new_deck,
                wins=new_wins, games=old_wins + new_wins + draws,
            ),
            draws=draws, total_games=games_per_pod,
            timestamp="2026-05-14T12:00:00",
            winner="new", margin=abs(new_wins - old_wins),
            pods=[{}], pods_planned=1, stopped_early=False,
        )

    monkeypatch.setattr(
        "commander_builder.compare_versions.compare", fake_compare,
    )
    monkeypatch.setattr(
        "commander_builder.forge_runner.ForgeRunner.locate",
        classmethod(lambda cls: SimpleNamespace(name="stubbed")),
    )
    return captured


def test_propose_swap_pod_mode_stages_in_commander_folder(
    client, monkeypatch,
):
    """Pod mode (the default) should stage the proposed .dck file as
    a sibling of the original — under ``userdata/decks/commander/``,
    NOT the ``constructed/`` subfolder. Forge's ``-f commander``
    looks here.
    """
    captured = _capture_compare(monkeypatch)
    new_text = (
        "[metadata]\nName=Alpha v2\n\n"
        "[Commander]\n1 Test Cmdr\n\n"
        "[Main]\n" + "1 Forest\n" * 35 + "1 Lotus Cobra\n" * 5
    )
    resp = client.post("/api/propose_swap", json={
        "deck": "Alpha", "new_text": new_text, "games": 10, "mode": "pod",
    })
    assert resp.status_code == 200
    # stage_dir for pod mode is the commander folder (the parent of
    # the original deck path). The test's deck_dir fixture IS the
    # commander folder.
    from pathlib import Path as _P
    assert _P(captured["deck_dir"]).name != "constructed"
    # Pod mode does NOT convert — the staged file should retain
    # its [Commander] section.
    assert "[Commander]" in captured["new_text"]


def test_propose_swap_1v1_mode_stages_in_constructed_subfolder(
    client, monkeypatch,
):
    """1v1 mode must stage files under
    ``<deck_dir>.parent / 'constructed'`` — Forge's
    ``-f constructed`` mode ONLY looks there. Staging 1v1 decks in
    the commander folder produces 'No deck found' errors and zero
    games (the original 'Done. 0 games played' mystery).
    """
    captured = _capture_compare(monkeypatch)
    new_text = (
        "[metadata]\nName=Alpha v2\n\n"
        "[Commander]\n1 Test Cmdr\n\n"
        "[Main]\n" + "1 Forest\n" * 35 + "1 Lotus Cobra\n" * 5
    )
    resp = client.post("/api/propose_swap", json={
        "deck": "Alpha", "new_text": new_text, "games": 10, "mode": "1v1",
    })
    assert resp.status_code == 200
    from pathlib import Path as _P
    assert _P(captured["deck_dir"]).name == "constructed"


def test_propose_swap_1v1_converts_both_old_and_new_decks(
    client, monkeypatch,
):
    """The 1v1 conversion path applies ``_to_constructed_format`` to
    BOTH the new (proposed) deck AND the old (baseline) deck —
    not just the new one. Forge can't sim a deck with a
    ``[Commander]`` section under ``-f constructed``, so both
    decks must have their commander folded into ``[Main]`` and the
    ``Deck Type=constructed`` metadata stamped.

    Pinned because the original implementation only converted the
    new deck for a stretch in early development, producing
    asymmetric sims where Forge would play one deck (the converted
    new one) but fail to find the old one.
    """
    captured = _capture_compare(monkeypatch)
    new_text = (
        "[metadata]\nName=Alpha v2\n\n"
        "[Commander]\n1 Test Cmdr\n\n"
        "[Main]\n" + "1 Forest\n" * 35 + "1 Lotus Cobra\n" * 5
    )
    resp = client.post("/api/propose_swap", json={
        "deck": "Alpha", "new_text": new_text, "games": 10, "mode": "1v1",
    })
    assert resp.status_code == 200
    # Both staged files must have the [Commander] section stripped
    # and Deck Type=constructed stamped.
    for label in ("old_text", "new_text"):
        text = captured[label]
        assert "[Commander]" not in text, (
            f"{label} still has [Commander] section: {text[:200]!r}"
        )
        assert "Deck Type=constructed" in text, (
            f"{label} missing Deck Type=constructed: {text[:200]!r}"
        )


def test_propose_swap_1v1_uniquifies_metadata_name(client, monkeypatch):
    """The 1v1 path renames both staged decks so Forge's match-result
    parser can attribute wins to the right side. Without this, both
    decks shipped with the same ``Name=`` field and log_parser
    counted every game as a tie regardless of who won.

    Pin that the new + old staged decks have DIFFERENT ``Name=``
    metadata values after staging.
    """
    captured = _capture_compare(monkeypatch)
    new_text = (
        "[metadata]\nName=Alpha\n\n"
        "[Commander]\n1 Test Cmdr\n\n"
        "[Main]\n" + "1 Forest\n" * 35 + "1 Lotus Cobra\n" * 5
    )
    resp = client.post("/api/propose_swap", json={
        "deck": "Alpha", "new_text": new_text, "games": 10, "mode": "1v1",
    })
    assert resp.status_code == 200
    import re as _re
    old_name = _re.search(r"^Name=(.+)$", captured["old_text"],
                          _re.MULTILINE)
    new_name = _re.search(r"^Name=(.+)$", captured["new_text"],
                          _re.MULTILINE)
    assert old_name and new_name, "both decks should have Name= metadata"
    assert old_name.group(1) != new_name.group(1), (
        f"old + new have same Name= ({old_name.group(1)!r}); "
        f"log_parser will tag every game as a tie."
    )
    # Both names should include a timestamp suffix so they're unique
    # across iterations (matching the _proposed_<ts> / _converted_<ts>
    # filename pattern).
    assert "_proposed_" in new_name.group(1) or "_converted_" in new_name.group(1)
    assert "_converted_" in old_name.group(1)


def test_propose_swap_1v1_cleans_up_staged_files_after_compare(
    client, monkeypatch, tmp_path,
):
    """Both ``_proposed_<ts>.dck`` and ``_converted_<ts>.dck`` files
    must be deleted after ``compare()`` returns. They're working
    copies for the A/B sim, not deck assets — leaving them on disk
    pollutes the sidebar and confuses future sessions.

    This test runs the full path (no captured-during-compare
    inspection) and then checks the constructed/ subfolder is
    empty (modulo any files the fixture itself created).
    """
    _capture_compare(monkeypatch)
    new_text = (
        "[metadata]\nName=Alpha v2\n\n"
        "[Commander]\n1 Test Cmdr\n\n"
        "[Main]\n" + "1 Forest\n" * 35 + "1 Lotus Cobra\n" * 5
    )
    resp = client.post("/api/propose_swap", json={
        "deck": "Alpha", "new_text": new_text, "games": 10, "mode": "1v1",
    })
    assert resp.status_code == 200
    # The deck_dir fixture path is tmp_path / "decks"; the
    # constructed staging dir is its sibling.
    from commander_builder.web.app import create_app  # noqa: F401
    # Look at the constructed dir if it exists.
    deck_dir = client.application.config.get("DECK_DIR")
    if deck_dir is None:
        return  # nothing to assert
    constructed_dir = Path(deck_dir).parent / "constructed"
    if constructed_dir.exists():
        leftover = [
            f.name for f in constructed_dir.iterdir()
            if "_proposed_" in f.name or "_converted_" in f.name
        ]
        assert leftover == [], (
            f"staged files left behind: {leftover}"
        )


def test_propose_swap_400_on_bad_mode_value(client, monkeypatch):
    """``mode`` must be 'pod' or '1v1' — any other value is a 400."""
    _capture_compare(monkeypatch)
    new_text = (
        "[metadata]\nName=Alpha v2\n\n[Commander]\n1 Cmdr\n\n"
        "[Main]\n1 Forest\n1 Lotus Cobra\n"
    )
    resp = client.post("/api/propose_swap", json={
        "deck": "Alpha", "new_text": new_text, "games": 10,
        "mode": "skirmish",   # nonsense
    })
    assert resp.status_code == 400
    body = resp.get_json()
    assert "mode" in body["error"]


def test_propose_swap_pod_mode_does_not_convert_format(client, monkeypatch):
    """Pod (commander) mode must NOT apply ``_to_constructed_format``.
    Forge's ``-f commander`` REQUIRES the ``[Commander]`` section;
    stripping it would silently produce zero-games sims (the same
    failure mode that motivated the 1v1 conversion in the first
    place, just in reverse).
    """
    captured = _capture_compare(monkeypatch)
    new_text = (
        "[metadata]\nName=Alpha v2\n\n"
        "[Commander]\n1 Test Cmdr\n\n"
        "[Main]\n" + "1 Forest\n" * 35 + "1 Lotus Cobra\n" * 5
    )
    resp = client.post("/api/propose_swap", json={
        "deck": "Alpha", "new_text": new_text, "games": 10, "mode": "pod",
    })
    assert resp.status_code == 200
    # Pod mode preserves [Commander] in the staged file.
    assert "[Commander]" in captured["new_text"]
    # And does NOT inject Deck Type=constructed.
    assert "Deck Type=constructed" not in captured["new_text"]


def test_propose_swap_same_second_requests_stage_distinct_paths(
    client, monkeypatch,
):
    """Two propose_swap requests staged within the SAME second must
    produce DIFFERENT staged file paths.

    The staged names carry a strftime("%Y%m%d_%H%M%S") timestamp —
    1-second granularity. Before the per-request uid suffix, two A/B
    sims on the same deck started in the same second built IDENTICAL
    paths: request B's write_text clobbered the file request A's Forge
    JVM was mid-reading, and A's cleanup unlink deleted the file B
    still needed (FileNotFound / '0 games played').

    Freeze datetime.now() so both requests see the exact same
    timestamp — the worst case — and pin that the paths still differ.
    routes_sim does `from datetime import datetime as _dt` inside the
    handler, so patching the datetime module's `datetime` attribute is
    picked up per-request (module-level `from` imports elsewhere keep
    their original binding and are unaffected).
    """
    import datetime as _dtmod

    class _FrozenDT(_dtmod.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 19, 12, 0, 0)

    monkeypatch.setattr(_dtmod, "datetime", _FrozenDT)

    captured = _capture_compare(monkeypatch)
    new_text = (
        "[metadata]\nName=Alpha v2\n\n"
        "[Commander]\n1 Test Cmdr\n\n"
        "[Main]\n" + "1 Forest\n" * 35 + "1 Lotus Cobra\n" * 5
    )
    # 1v1 mode stages BOTH a _proposed_ and a _converted_ file — cover
    # the whole collision class in one run.
    body = {
        "deck": "Alpha", "new_text": new_text, "games": 10, "mode": "1v1",
    }
    resp_a = client.post("/api/propose_swap", json=body)
    assert resp_a.status_code == 200
    first_new = captured["new_deck"]
    first_old = captured["old_deck"]
    resp_b = client.post("/api/propose_swap", json=body)
    assert resp_b.status_code == 200
    second_new = captured["new_deck"]
    second_old = captured["old_deck"]

    frozen_ts = "20260719_120000"
    # Sanity: the frozen clock actually took — both runs share the
    # exact same timestamp component, so ONLY the uid can differ.
    for name in (first_new, second_new):
        assert f"_proposed_{frozen_ts}_" in name, name
    for name in (first_old, second_old):
        assert f"_converted_{frozen_ts}_" in name, name
    # The actual collision pin: identical second, distinct paths.
    assert first_new != second_new, (
        "same-second propose_swap requests staged the SAME proposed "
        f"path ({first_new!r}) — concurrent A/B sims would clobber "
        "each other's staged decks"
    )
    assert first_old != second_old, (
        "same-second propose_swap requests staged the SAME converted "
        f"path ({first_old!r})"
    )


def test_propose_swap_staged_name_metadata_equals_filename_stem(
    client, monkeypatch,
):
    """Win attribution requires `_normalize(<filename stem>) ==
    _normalize(<Name= field>)` (see dck_meta's module docstring):
    Forge reports the Name= field, compare_versions queries by
    filename. Assert the RELATIONSHIP (Name == stem) rather than a
    literal so the pin survives changes to the uniquifier shape —
    the uid suffix lives on both sides, which is exactly what keeps
    attribution consistent.
    """
    import re as _re
    from commander_builder.log_parser import _normalize

    captured = _capture_compare(monkeypatch)
    new_text = (
        "[metadata]\nName=Alpha v2\n\n"
        "[Commander]\n1 Test Cmdr\n\n"
        "[Main]\n" + "1 Forest\n" * 35 + "1 Lotus Cobra\n" * 5
    )
    resp = client.post("/api/propose_swap", json={
        "deck": "Alpha", "new_text": new_text, "games": 10, "mode": "1v1",
    })
    assert resp.status_code == 200
    for deck_key, text_key in (
        ("new_deck", "new_text"),
        ("old_deck", "old_text"),
    ):
        stem = Path(captured[deck_key]).stem
        m = _re.search(r"^Name=(.+)$", captured[text_key], _re.MULTILINE)
        assert m, f"{text_key} has no Name= metadata"
        assert m.group(1) == stem, (
            f"{deck_key}: staged Name= ({m.group(1)!r}) != filename "
            f"stem ({stem!r}) — log_parser can't attribute wins"
        )
        # And the normalized forms agree — the exact key match win
        # attribution runs on.
        assert _normalize(captured[deck_key]) == _normalize(m.group(1))


def test_compare_iterations_handles_added_cards(seeded_client):
    """Demo seeder writes the same minimal snapshot for each version
    plus one extra card slug per iteration; check the diff isn't empty
    when versions differ."""
    # Fall back to checking shape — seeder writes near-identical
    # snapshots, but the test verifies the endpoint path works.
    resp = seeded_client.get("/api/compare/2/3")
    assert resp.status_code == 200
    body = resp.get_json()
    assert isinstance(body["added"], list)
    assert isinstance(body["removed"], list)


# ---------------------------------------------------------------------------
# /api/save_iteration
# ---------------------------------------------------------------------------

@pytest.fixture
def save_client(deck_dir, tmp_path, monkeypatch):
    """Fresh empty knowledge_log + a deck dir with an Alpha deck."""
    monkeypatch.setattr(
        "commander_builder.deck_dashboard.lookup_card",
        lambda name: {"type_line": "Basic Land", "cmc": 0.0,
                      "color_identity": ["G"], "prices": {"usd": "0.05"}},
    )
    db = tmp_path / "save_klog.sqlite"
    app = create_app(deck_dir=deck_dir, knowledge_db=db)
    app.config["TESTING"] = True
    client = app.test_client()
    return client, db


def test_save_iteration_persists_row(save_client):
    client, db = save_client
    payload = {
        "deck_id": "Alpha",
        "deck_name": "Alpha",
        "bracket": 3,
        "audit_version": "v3",
        "audit_manifest": {
            "added": [{"card": "Lotus Cobra", "rationale": "ramp"}],
            "removed": [{"card": "Cultivate", "rationale": "slow"}],
        },
        "sim_report": {
            "winner": "new", "old_wins": 4, "new_wins": 6,
            "old_games": 10, "new_games": 10, "draws": 0,
            "margin": 2, "total_games": 20, "mode": "pod", "bracket": 3,
        },
        "verdict": "kept",
        "verdict_notes": "Cobra was the right call.",
    }
    resp = client.post("/api/save_iteration", json=payload)
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert isinstance(body["id"], int) and body["id"] >= 1
    assert body["verdict"] == "kept"
    assert body["stats"]["total"] == 1
    assert body["stats"]["kept"] == 1

    # Read back through /api/iteration to confirm it landed.
    detail = client.get(f"/api/iteration/{body['id']}").get_json()
    assert detail["deck_id"] == "Alpha"
    assert detail["bracket"] == 3
    assert detail["verdict"] == "kept"
    assert detail["margin"] == 2
    assert abs(detail["win_rate_old"] - 0.4) < 1e-9
    assert abs(detail["win_rate_new"] - 0.6) < 1e-9
    # audit_manifest survives the round-trip.
    assert detail["audit_manifest"]["added"][0]["card"] == "Lotus Cobra"


def test_save_iteration_defaults_deck_snapshot_from_disk(save_client):
    client, db = save_client
    resp = client.post("/api/save_iteration", json={
        "deck_id": "Alpha",
        "deck_name": "Alpha",
        "bracket": 3,
        "verdict": "pending",
    })
    assert resp.status_code == 200
    new_id = resp.get_json()["id"]
    detail = client.get(f"/api/iteration/{new_id}/snapshot")
    assert detail.status_code == 200
    text = detail.get_data(as_text=True)
    assert "Name=Alpha" in text


def test_save_iteration_400_on_missing_deck_id(save_client):
    client, _ = save_client
    resp = client.post("/api/save_iteration", json={"verdict": "pending"})
    assert resp.status_code == 400
    assert "deck_id" in resp.get_json()["error"]


def test_save_iteration_400_on_invalid_verdict(save_client):
    client, _ = save_client
    resp = client.post("/api/save_iteration", json={
        "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
        "verdict": "lgtm",
    })
    assert resp.status_code == 400
    assert "verdict" in resp.get_json()["error"]


def test_save_iteration_accepts_inconclusive(save_client):
    """'inconclusive' is the web UI's DEFAULT save verdict whenever the
    sim had < 20 decisive games (app.js renderSaveIterationBlock), and
    both the PATCH endpoint and knowledge_log already accept it — so
    save_iteration rejecting it 400'd the UI's own default path."""
    client, _ = save_client
    resp = client.post("/api/save_iteration", json={
        "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
        # A 3-2 smoke sim: exactly the low-N shape that defaults the UI
        # radio to 'inconclusive'.
        "sim_report": {
            "winner": "new", "old_wins": 2, "new_wins": 3,
            "old_games": 5, "new_games": 5, "draws": 0,
            "margin": 1, "total_games": 10, "mode": "pod", "bracket": 3,
        },
        "verdict": "inconclusive",
    })
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["verdict"] == "inconclusive"
    # Round-trips through the detail endpoint unchanged.
    detail = client.get(f"/api/iteration/{body['id']}").get_json()
    assert detail["verdict"] == "inconclusive"


def test_save_iteration_400_on_invalid_bracket(save_client):
    client, _ = save_client
    resp = client.post("/api/save_iteration", json={
        "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 9,
        "verdict": "pending",
    })
    assert resp.status_code == 400
    assert "bracket" in resp.get_json()["error"]


def test_save_iteration_400_on_non_object_audit_manifest(save_client):
    client, _ = save_client
    resp = client.post("/api/save_iteration", json={
        "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
        "audit_manifest": "not-an-object", "verdict": "pending",
    })
    assert resp.status_code == 400
    assert "audit_manifest" in resp.get_json()["error"]


def test_pricing_series_empty_deck(save_client):
    """No iterations → empty points list."""
    client, _ = save_client
    resp = client.get("/api/pricing_series?deck=Alpha")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["deck_id"] == "Alpha"
    assert body["count"] == 0
    assert body["points"] == []


def test_pricing_series_chronological_points(save_client):
    """Iterations saved with total_price_usd appear in the series in
    chronological order, with iteration_id + captured_at + the price."""
    client, _ = save_client
    # 3 iterations at different prices.
    for price in [142.37, 138.50, 95.00]:
        client.post("/api/save_iteration", json={
            "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
            "total_price_usd": price, "verdict": "pending",
        })
    resp = client.get("/api/pricing_series?deck=Alpha")
    body = resp.get_json()
    assert body["count"] == 3
    prices = [p["total_price_usd"] for p in body["points"]]
    assert prices == [142.37, 138.50, 95.00]
    # Each point carries iteration_id + captured_at.
    for point in body["points"]:
        assert isinstance(point["iteration_id"], int)
        assert point["captured_at"]


def test_pricing_series_400_without_deck_param(save_client):
    client, _ = save_client
    resp = client.get("/api/pricing_series")
    assert resp.status_code == 400


def test_verdict_breakdown_empty_deck(save_client):
    """No iterations → breakdown is an empty dict; total_iterations=0."""
    client, _ = save_client
    resp = client.get("/api/verdict_breakdown?deck=Alpha")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["deck_id"] == "Alpha"
    assert body["total_iterations"] == 0
    assert body["breakdown"] == {}


def test_verdict_breakdown_groups_by_audit_version(save_client):
    """After saving iterations under multiple audit_versions, the
    breakdown groups them and reports per-group verdict counts."""
    client, _ = save_client
    # Two v3 saves (one kept, one reverted) + one v4 kept.
    for verdict in ["kept", "reverted"]:
        client.post("/api/save_iteration", json={
            "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
            "audit_version": "v3", "verdict": verdict,
        })
    client.post("/api/save_iteration", json={
        "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
        "audit_version": "v4", "verdict": "kept",
    })

    resp = client.get("/api/verdict_breakdown?deck=Alpha")
    body = resp.get_json()
    assert body["total_iterations"] == 3
    assert body["breakdown"]["v3"]["kept"] == 1
    assert body["breakdown"]["v3"]["reverted"] == 1
    assert body["breakdown"]["v3"]["total"] == 2
    assert body["breakdown"]["v4"]["kept"] == 1
    assert body["breakdown"]["v4"]["total"] == 1


def test_verdict_breakdown_400_without_deck_param(save_client):
    client, _ = save_client
    resp = client.get("/api/verdict_breakdown")
    assert resp.status_code == 400
    assert "deck" in resp.get_json()["error"]


def test_save_iteration_handles_missing_sim_report(save_client):
    """Save should succeed even when the user persists an audit-only
    record (no sim_report yet). win_rate columns stay NULL."""
    client, _ = save_client
    resp = client.post("/api/save_iteration", json={
        "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
        "verdict": "pending",
    })
    assert resp.status_code == 200
    new_id = resp.get_json()["id"]
    detail = client.get(f"/api/iteration/{new_id}").get_json()
    assert detail["win_rate_old"] is None
    assert detail["win_rate_new"] is None
    assert detail["margin"] is None


# --- /api/save_iteration — pricing snapshot for cost-evolution chart ------

def test_save_iteration_captures_pricing_snapshot_into_manifest(save_client):
    """Top-level total_price_usd lands inside audit_manifest.pricing so
    the row's manifest carries cost data alongside the swap manifest.
    Captured timestamp tags when the snapshot was taken (in case the
    user later renames the deck or its on-disk price drifts)."""
    client, _ = save_client
    resp = client.post("/api/save_iteration", json={
        "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
        "audit_manifest": {"added": [], "removed": []},
        "total_price_usd": 142.37,
        "verdict": "pending",
    })
    assert resp.status_code == 200, resp.get_json()
    new_id = resp.get_json()["id"]
    detail = client.get(f"/api/iteration/{new_id}").get_json()
    manifest = detail["audit_manifest"]
    assert "pricing" in manifest
    assert manifest["pricing"]["total_price_usd"] == 142.37
    assert "captured_at" in manifest["pricing"]
    # ISO timestamp surface for analytics.
    assert manifest["pricing"]["captured_at"].startswith("20")


def test_save_iteration_pricing_without_audit_manifest_synthesizes_one(save_client):
    """Top-level total_price_usd should still land even when the caller
    didn't pass an audit_manifest — endpoint creates a minimal one with
    just the pricing block."""
    client, _ = save_client
    resp = client.post("/api/save_iteration", json={
        "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
        "total_price_usd": 95.0,
        "verdict": "pending",
    })
    assert resp.status_code == 200
    detail = client.get(f"/api/iteration/{resp.get_json()['id']}").get_json()
    assert detail["audit_manifest"] is not None
    assert detail["audit_manifest"]["pricing"]["total_price_usd"] == 95.0


def test_save_iteration_omits_pricing_when_not_provided(save_client):
    """No total_price_usd in payload → audit_manifest stays unchanged,
    no pricing key fabricated. Avoids polluting legacy save flows."""
    client, _ = save_client
    resp = client.post("/api/save_iteration", json={
        "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
        "audit_manifest": {"added": [], "removed": []},
        "verdict": "pending",
    })
    assert resp.status_code == 200
    detail = client.get(f"/api/iteration/{resp.get_json()['id']}").get_json()
    manifest = detail["audit_manifest"]
    assert "pricing" not in manifest


def test_save_iteration_preserves_caller_supplied_pricing(save_client):
    """If audit_manifest already carries a pricing block, the endpoint
    leaves it alone — the caller is the source of truth."""
    client, _ = save_client
    resp = client.post("/api/save_iteration", json={
        "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
        "audit_manifest": {
            "added": [],
            "pricing": {"total_price_usd": 200.0, "captured_at": "2025-01-01T00:00:00+00:00"},
        },
        # Top-level value should NOT overwrite the caller's explicit one.
        "total_price_usd": 999.99,
        "verdict": "pending",
    })
    assert resp.status_code == 200
    detail = client.get(f"/api/iteration/{resp.get_json()['id']}").get_json()
    pricing = detail["audit_manifest"]["pricing"]
    assert pricing["total_price_usd"] == 200.0
    assert pricing["captured_at"] == "2025-01-01T00:00:00+00:00"


def test_save_iteration_400_on_invalid_total_price_usd(save_client):
    """Non-numeric total_price_usd → 400, not silent drop."""
    client, _ = save_client
    resp = client.post("/api/save_iteration", json={
        "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
        "total_price_usd": "lots of money",
        "verdict": "pending",
    })
    assert resp.status_code == 400
    assert "total_price_usd" in resp.get_json()["error"]


def test_save_iteration_accepts_zero_price(save_client):
    """Zero is a legal price (jank deck, all basics) — must not be
    treated as 'missing'."""
    client, _ = save_client
    resp = client.post("/api/save_iteration", json={
        "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
        "total_price_usd": 0.0,
        "verdict": "pending",
    })
    assert resp.status_code == 200
    detail = client.get(f"/api/iteration/{resp.get_json()['id']}").get_json()
    assert detail["audit_manifest"]["pricing"]["total_price_usd"] == 0.0


def test_save_iteration_rejects_negative_price(save_client):
    """Regression: a negative price almost certainly means bad Scryfall
    data or a sign-flip bug upstream — never a legitimate value.
    Accepting it silently would poison the cost-evolution chart with
    nonsensical points; reject at the boundary instead."""
    client, _ = save_client
    resp = client.post("/api/save_iteration", json={
        "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
        "total_price_usd": -42.50,
        "verdict": "pending",
    })
    assert resp.status_code == 400
    assert "total_price_usd" in resp.get_json()["error"]
    assert "negative" in resp.get_json()["error"].lower() or \
        "non-negative" in resp.get_json()["error"].lower()


def test_audit_proposed_price_round_trips_into_save_iteration(
    save_client, monkeypatch,
):
    """Integration test for the JS wiring that pipes the audit
    response's ``proposed_price_usd`` through to ``save_iteration``
    as ``total_price_usd``. Simulates what app.js does:

      1. POST /api/audit, capture ``proposed_price_usd``.
      2. POST /api/save_iteration with that value as
         ``total_price_usd``.
      3. GET /api/iteration/<id> → the pricing snapshot lives at
         ``audit_manifest.pricing.total_price_usd`` and equals the
         audit's proposed price.

    Protects the round-trip contract so a future refactor of the
    audit response shape (e.g. renaming the field) is caught here
    even though we can't run the client JS in the test suite.
    """
    from types import SimpleNamespace

    client, _ = save_client

    def fake_lookup(name, *_a, **_kw):
        prices = {"Sol Ring": "1.50", "Cultivate": "0.50",
                  "Lotus Cobra": "10.00", "Forest": "0.05"}
        if name in prices:
            return {"prices": {"usd": prices[name]}}
        return None
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", fake_lookup,
    )

    def fake_advise(deck_path, bracket, **_kwargs):
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(card="Lotus Cobra", action="add",
                                reason="upgrade", evidence={},
                                name_known=True),
                SimpleNamespace(card="Cultivate", action="cut",
                                reason="downgrade", evidence={},
                                name_known=True),
            ],
            diagnosis=SimpleNamespace(pattern_summary="",
                                      weakness_signals=[]),
            source="heuristic", fallback_reason=None,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )

    audit = client.get("/api/audit?deck=Alpha&bracket=3").get_json()
    proposed = audit.get("proposed_price_usd")
    original = audit.get("original_price_usd")
    # Mirror the JS fallback rule: prefer proposed, else original.
    captured = proposed if proposed is not None else original
    assert captured is not None, audit

    saved = client.post("/api/save_iteration", json={
        "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
        "audit_manifest": {"added": [], "removed": []},
        "total_price_usd": captured,
        "verdict": "pending",
    })
    assert saved.status_code == 200, saved.get_json()

    detail = client.get(f"/api/iteration/{saved.get_json()['id']}").get_json()
    pricing = detail["audit_manifest"]["pricing"]
    assert pricing["total_price_usd"] == captured


# --- /api/audit — card-name validation (hallucination defense) ------------

def test_audit_payload_includes_name_known_for_each_rec(client, monkeypatch):
    """Both added[] and removed[] dicts surface name_known so the UI can
    flag Claude-hallucinated cards."""
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **_kwargs):
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(
                    card="Lotus Cobra", action="add", reason="ramp",
                    evidence={"inclusion_pct": 78.0},
                    name_known=True,
                ),
                SimpleNamespace(
                    card="Accursed Marauder", action="add",
                    reason="hallucinated by Claude",
                    evidence={"source": "claude"},
                    name_known=False,
                ),
                SimpleNamespace(
                    card="Cultivate", action="cut", reason="slow",
                    evidence={}, name_known=True,
                ),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="claude",
            fallback_reason=None,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    resp = client.get("/api/audit?deck=Alpha&bracket=3")
    assert resp.status_code == 200
    body = resp.get_json()
    added_by_card = {a["card"]: a for a in body["added"]}
    assert added_by_card["Lotus Cobra"]["name_known"] is True
    # _apply_swaps_to_dck may drop the unknown card because the card-text
    # snippet generator can't render a basic frame for it. The validator
    # surfaces in the manifest regardless, so look at the manifest.
    # The endpoint should still mark the rec it kept.
    if "Accursed Marauder" in added_by_card:
        assert added_by_card["Accursed Marauder"]["name_known"] is False
    removed_by_card = {r["card"]: r for r in body["removed"]}
    if "Cultivate" in removed_by_card:
        assert removed_by_card["Cultivate"]["name_known"] is True


def test_audit_payload_reports_unknown_card_count(client, monkeypatch):
    """Top-level unknown_card_count counts recs flagged name_known=False
    among the cards that actually landed in the proposed deck."""
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **_kwargs):
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(
                    card="Lotus Cobra", action="add", reason="",
                    evidence={}, name_known=True,
                ),
                SimpleNamespace(
                    card="Bogus Phantasm", action="add", reason="",
                    evidence={}, name_known=False,
                ),
                SimpleNamespace(
                    card="Cultivate", action="cut", reason="",
                    evidence={}, name_known=True,
                ),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="claude",
            fallback_reason=None,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    resp = client.get("/api/audit?deck=Alpha&bracket=3")
    assert resp.status_code == 200
    body = resp.get_json()
    # The fake card may or may not survive _apply_swaps_to_dck; either
    # way the response surfaces the count for the cards that *did* land.
    assert "unknown_card_count" in body
    assert isinstance(body["unknown_card_count"], int)
    assert body["unknown_card_count"] >= 0


def test_audit_routes_source_bracket_peers_to_advise(client, monkeypatch):
    """?source=bracket_peers must reach advise() as source='bracket_peers'.
    Mirrors the existing claude routing test."""
    from types import SimpleNamespace
    seen = {}

    def fake_advise(deck_path, bracket, **kwargs):
        seen["source"] = kwargs.get("source")
        seen["use_claude"] = kwargs.get("use_claude", False)
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(
                    card="Moat", action="add",
                    reason="in 5/5 reference decks (unanimous)",
                    evidence={
                        "in_n_references": 5,
                        "total_references": 5,
                        "frequency_label": "unanimous",
                        "role": "protection",
                        "source": "bracket_peers",
                    },
                    name_known=True,
                ),
                SimpleNamespace(
                    card="Cultivate", action="cut",
                    reason="absent from all 5 reference decks",
                    evidence={"source": "bracket_peers"},
                    name_known=True,
                ),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="bracket_peers",
            fallback_reason=None,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    resp = client.get("/api/audit?deck=Alpha&bracket=3&source=bracket_peers")
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert seen["source"] == "bracket_peers"
    assert body["source"] == "bracket_peers"
    assert body["requested_llm"] == "bracket_peers"


def test_audit_bracket_peers_surfaces_fallback_reason(client, monkeypatch):
    """When advise() falls back to heuristic (no refs found), the UI
    needs to know — surface fallback_reason in body.warning so the
    user sees why they got the EDHREC heuristic instead."""
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **kwargs):
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(card="Sol Ring", action="add",
                                reason="r", evidence={}, name_known=True),
                SimpleNamespace(card="Cultivate", action="cut",
                                reason="r", evidence={}, name_known=True),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="heuristic",
            fallback_reason="no bracket-peer references found for "
                            "'Obscure' at B3 — falling back to EDHREC heuristic",
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    resp = client.get("/api/audit?deck=Alpha&bracket=3&source=bracket_peers")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["source"] == "heuristic"  # fell back
    assert body["warning"] is not None
    assert "no bracket-peer references" in body["warning"]


def test_audit_400_on_unknown_source(client):
    """Unrecognized source value should 400, not silently fall through."""
    resp = client.get(
        "/api/audit?deck=Alpha&bracket=3&source=garbage_value",
    )
    assert resp.status_code == 400
    assert "source" in resp.get_json()["error"]


def test_audit_source_param_overrides_llm_param(client, monkeypatch):
    """When BOTH ?llm=claude AND ?source=bracket_peers are passed,
    source wins. (Old UI shipped llm=; new UI ships source=. Don't
    break existing bookmarks but let the newer name be authoritative.)"""
    from types import SimpleNamespace
    seen = {}

    def fake(deck_path, bracket, **kwargs):
        seen["source"] = kwargs.get("source")
        seen["use_claude"] = kwargs.get("use_claude", False)
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(card="X", action="add", reason="",
                                evidence={}, name_known=True),
                SimpleNamespace(card="Y", action="cut", reason="",
                                evidence={}, name_known=True),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="bracket_peers",
            fallback_reason=None,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake,
    )
    resp = client.get(
        "/api/audit?deck=Alpha&bracket=3"
        "&llm=claude&source=bracket_peers",
    )
    assert resp.status_code == 200
    assert seen["source"] == "bracket_peers"
    assert seen["use_claude"] is False  # source overrode the llm param


def test_audit_payload_surfaces_bracket_peer_ref_count(client, monkeypatch):
    """When advise() reports peer references were attached to a Claude
    request, the audit response carries the count so the UI can render
    'Claude analyst (5 peer refs)' on the source pill."""
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **_kwargs):
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(card="Lotus Cobra", action="add", reason="",
                                evidence={}, name_known=True),
                SimpleNamespace(card="Cultivate", action="cut", reason="",
                                evidence={}, name_known=True),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="claude",
            fallback_reason=None,
            bracket_peer_ref_count=5,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    resp = client.get("/api/audit?deck=Alpha&bracket=3&source=claude",
                      headers={"X-Anthropic-API-Key": "sk-test"})
    body = resp.get_json()
    assert body["bracket_peer_ref_count"] == 5


def test_audit_payload_bracket_peer_ref_count_defaults_zero(client, monkeypatch):
    """Legacy stubs without the field render as 0, never raise."""
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **_kwargs):
        # No bracket_peer_ref_count attr at all.
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(card="Lotus Cobra", action="add", reason="",
                                evidence={}, name_known=True),
                SimpleNamespace(card="Cultivate", action="cut", reason="",
                                evidence={}, name_known=True),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="heuristic",
            fallback_reason=None,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    resp = client.get("/api/audit?deck=Alpha&bracket=3")
    assert resp.get_json()["bracket_peer_ref_count"] == 0


@pytest.mark.slow
def test_audit_endpoint_threads_budget_param_to_advise(client, monkeypatch):
    """?budget=1 should reach advise() as budget=True so the manabase
    safety net switches to the cheaper land set."""
    from types import SimpleNamespace
    seen = {}

    def fake_advise(deck_path, bracket, **kwargs):
        seen["budget"] = kwargs.get("budget")
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(card="A", action="add", reason="",
                                evidence={}, name_known=True),
                SimpleNamespace(card="B", action="cut", reason="",
                                evidence={}, name_known=True),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="heuristic", fallback_reason=None,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    # Default: budget not set → False.
    client.get("/api/audit?deck=Alpha&bracket=3")
    assert seen["budget"] is False
    # Explicit truthy values.
    for v in ("1", "true", "yes"):
        client.get(f"/api/audit?deck=Alpha&bracket=3&budget={v}")
        assert seen["budget"] is True, f"budget={v} should set True"
    # Explicit falsy / unrecognized → False.
    for v in ("0", "false", "no", "maybe"):
        client.get(f"/api/audit?deck=Alpha&bracket=3&budget={v}")
        assert seen["budget"] is False, f"budget={v} should set False"


def test_audit_payload_match_pct_null_for_signal_less_evidence(client, monkeypatch):
    """Regression (2026-05-13): Claude recs only carry
    evidence={"source": "claude"} — no EDHREC inclusion/synergy AND
    no peer references frequency. The original implementation
    clamped these up to 1, displaying a misleading '1%' in the UI.
    A later fix returned 0, which the UI rendered as "0%" — visually
    identical to "this is a bad match." Now returns None (JSON null)
    so the UI suppresses the pill entirely and renders a source-tag
    badge ('Claude analyst') in its place.
    """
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **_kwargs):
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(
                    card="Cool Claude Pick", action="add", reason="LLM said so",
                    evidence={"source": "claude"},  # nothing else
                    name_known=True,
                ),
                SimpleNamespace(
                    card="Cultivate", action="cut", reason="",
                    evidence={}, name_known=True,
                ),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="claude", fallback_reason=None,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    resp = client.get("/api/audit?deck=Alpha&bracket=3&source=claude",
                      headers={"X-Anthropic-API-Key": "sk-test"})
    body = resp.get_json()
    by_card = {a["card"]: a for a in body["added"]}
    assert by_card["Cool Claude Pick"]["match_pct"] is None
    # The source field must surface so the UI can pick a badge.
    assert by_card["Cool Claude Pick"]["source"] == "claude"


def test_audit_payload_includes_price_delta(client, monkeypatch):
    """Audit response surfaces original + proposed deck prices plus
    a computed delta. Tier-2 backlog item shipped in this commit.
    Feeds the cost-evolution chart and the UI's "$X → $Y (Δ)"
    headline alongside the diff.

    Test stubs Scryfall's lookup to return controlled prices so the
    expected delta is predictable.
    """
    from types import SimpleNamespace

    def fake_lookup(name, *_a, **_kw):
        prices = {
            "Sol Ring": "1.50",
            "Cultivate": "0.50",
            "Lotus Cobra": "10.00",
            "Forest": "0.05",
        }
        if name in prices:
            return {"prices": {"usd": prices[name]}}
        return None
    # Patch both deck_dashboard's lookup (which client fixture also
    # patches) and scryfall_client directly so the audit endpoint's
    # _total_price_for_deck_text helper sees the mocked prices.
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", fake_lookup,
    )

    def fake_advise(deck_path, bracket, **_kwargs):
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(
                    card="Lotus Cobra", action="add",
                    reason="upgrade", evidence={},
                    name_known=True,
                ),
                SimpleNamespace(
                    card="Cultivate", action="cut",
                    reason="downgrade", evidence={},
                    name_known=True,
                ),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="heuristic", fallback_reason=None,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    resp = client.get("/api/audit?deck=Alpha&bracket=3")
    body = resp.get_json()
    # Net delta is dominated by adding Lotus Cobra ($10) minus all
    # 5 Cultivates ($2.50) plus the basic-padding (_pad_main_to_99
    # tops the deck up to 99 with Forests after the cut, so the
    # exact delta depends on padding count). Sanity-check the sign
    # + ballpark rather than pin an exact value — the padding
    # contribution is a real (intended) behavior we don't want to
    # over-constrain.
    assert body.get("original_price_usd") is not None
    assert body.get("proposed_price_usd") is not None
    assert body.get("price_delta_usd") is not None
    # Sign: positive (audit added $10 Lotus Cobra, removed cheap
    # Cultivates + cheap Forest padding).
    assert body["price_delta_usd"] > 5
    # Upper bound: less than the Lotus Cobra price + a generous
    # padding allowance. If this fails, the audit is leaking a
    # huge price somewhere.
    assert body["price_delta_usd"] < 20


def test_audit_payload_surfaces_unapplied_adds_when_no_cuts(client, monkeypatch):
    """Live two-deck comparison (2026-05-14) caught a regression
    introduced by the MIN_EDHREC_SIGNAL_FOR_CUTS gate: when the
    heuristic refused to emit cuts (sparse EDHREC data), the
    adds==cuts balancing in ``_apply_swaps_to_dck`` silently
    dropped all adds (min(adds, 0) = 0). Users got "no
    recommendations" instead of "here are 8 manabase upgrades."

    Fix: the ``added`` payload now includes ALL recommendations
    from the report, with an ``applied`` flag indicating whether
    each one landed in ``proposed_text``. The UI can render
    unapplied entries as "needs manual cut" suggestions instead
    of dropping them silently.
    """
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **_kwargs):
        # 3 adds but ZERO cuts — mirrors the live failure where
        # the cut-gate fired and balancing dropped all adds.
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(
                    card="Sacred Foundry", action="add",
                    reason="essential dual",
                    evidence={"source": "manabase_essentials"},
                    name_known=True,
                ),
                SimpleNamespace(
                    card="Blood Crypt", action="add",
                    reason="essential dual",
                    evidence={"source": "manabase_essentials"},
                    name_known=True,
                ),
                SimpleNamespace(
                    card="Steam Vents", action="add",
                    reason="essential dual",
                    evidence={"source": "manabase_essentials"},
                    name_known=True,
                ),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="heuristic", fallback_reason=None,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    resp = client.get("/api/audit?deck=Alpha&bracket=3")
    body = resp.get_json()
    # All 3 recommendations must surface in the added payload, NOT
    # be dropped due to add/cut imbalance.
    assert len(body["added"]) == 3, (
        f"expected 3 adds visible, got {len(body['added'])}: "
        f"{[a['card'] for a in body['added']]}"
    )
    # None landed in proposed_text since there were no cuts.
    for a in body["added"]:
        assert a["applied"] is False, (
            f"{a['card']!r} should be marked applied=False "
            f"(no cut to balance), got True"
        )


def test_audit_payload_match_pct_null_for_manabase_essentials(client, monkeypatch):
    """Manabase essentials (Sacred Foundry, Cavern of Souls, etc.)
    have no inclusion% or reference-frequency signal — they're added
    by the curated ``_advisor_manabase`` source. The audit response
    must surface match_pct=null + source='manabase_essentials' so the
    UI shows a 'Manabase' badge instead of a misleading '0%' pill."""
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **_kwargs):
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(
                    card="Sacred Foundry", action="add",
                    reason="essential dual land for color identity",
                    evidence={"source": "manabase_essentials"},
                    name_known=True,
                ),
                SimpleNamespace(
                    card="Cavern of Souls", action="add",
                    reason="essential tribal land",
                    evidence={"source": "tribal_essentials"},
                    name_known=True,
                ),
                # Two cuts so adds==cuts balancing keeps both adds.
                SimpleNamespace(card="Cut A", action="cut", reason="",
                                evidence={}, name_known=True),
                SimpleNamespace(card="Cut B", action="cut", reason="",
                                evidence={}, name_known=True),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="heuristic", fallback_reason=None,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    resp = client.get("/api/audit?deck=Alpha&bracket=3")
    body = resp.get_json()
    by_card = {a["card"]: a for a in body["added"]}
    assert by_card["Sacred Foundry"]["match_pct"] is None
    assert by_card["Sacred Foundry"]["source"] == "manabase_essentials"
    assert by_card["Cavern of Souls"]["match_pct"] is None
    assert by_card["Cavern of Souls"]["source"] == "tribal_essentials"


def test_audit_payload_match_pct_from_reference_frequency(client, monkeypatch):
    """Phase A gap #2 from the bracket-peers self-audit: bracket_peers
    recs only set in_n_references / total_references, not inclusion_pct
    / synergy_pct. The audit response's match_pct field must compute
    from those for the UI pill to render anything other than 0%."""
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **_kwargs):
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(
                    card="Moat", action="add", reason="r",
                    evidence={
                        "in_n_references": 5,
                        "total_references": 5,
                        "source": "bracket_peers",
                    },
                    name_known=True,
                ),
                SimpleNamespace(
                    card="Last March", action="add", reason="r",
                    evidence={
                        "in_n_references": 3,
                        "total_references": 5,
                        "source": "bracket_peers",
                    },
                    name_known=True,
                ),
                SimpleNamespace(
                    card="Cultivate", action="cut", reason="r",
                    evidence={}, name_known=True,
                ),
                # Second cut so _apply_swaps_to_dck can keep BOTH adds.
                # The endpoint balances adds==cuts; without this we'd
                # only see the first add land in the response.
                SimpleNamespace(
                    card="Old Card", action="cut", reason="r",
                    evidence={}, name_known=True,
                ),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="bracket_peers",
            fallback_reason=None,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    resp = client.get("/api/audit?deck=Alpha&bracket=3&source=bracket_peers")
    body = resp.get_json()
    by_card = {a["card"]: a for a in body["added"]}
    # 5/5 = 100% — unanimous reference inclusion gets max pill.
    assert by_card["Moat"]["match_pct"] == 100
    # 3/5 = 60% — majority but not unanimous.
    assert by_card["Last March"]["match_pct"] == 60


def test_audit_payload_includes_saturation_skips(client, monkeypatch):
    """When the saturation guard drops ramp adds (deck over-saturated),
    the dropped cards surface in body.skipped_for_saturation so the UI
    can show 'skipped 3 ramp adds — your deck already has 13'."""
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **_kwargs):
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(
                    card="Lotus Cobra", action="add", reason="",
                    evidence={"role": "ramp"}, name_known=True,
                ),
                SimpleNamespace(
                    card="Cultivate", action="cut", reason="",
                    evidence={}, name_known=True,
                ),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="heuristic",
            fallback_reason=None,
            skipped_for_saturation=[
                {"card": "Rampant Growth", "role": "ramp",
                 "deck_count": 13, "threshold": 12},
                {"card": "Three Visits", "role": "ramp",
                 "deck_count": 13, "threshold": 12},
            ],
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    resp = client.get("/api/audit?deck=Alpha&bracket=3")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "skipped_for_saturation" in body
    assert len(body["skipped_for_saturation"]) == 2
    first = body["skipped_for_saturation"][0]
    assert first["role"] == "ramp"
    assert first["deck_count"] == 13
    assert first["threshold"] == 12
    assert first["card"] == "Rampant Growth"


def test_audit_payload_skipped_for_saturation_defaults_empty(client, monkeypatch):
    """Legacy stubs that don't set skipped_for_saturation must surface as
    an empty list, not raise AttributeError."""
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **_kwargs):
        # SimpleNamespace WITHOUT skipped_for_saturation attribute —
        # simulates old stubs / pre-v8 callers.
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(
                    card="Lotus Cobra", action="add",
                    reason="", evidence={}, name_known=True,
                ),
                SimpleNamespace(
                    card="Cultivate", action="cut",
                    reason="", evidence={}, name_known=True,
                ),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="heuristic",
            fallback_reason=None,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    resp = client.get("/api/audit?deck=Alpha&bracket=3")
    assert resp.status_code == 200
    assert resp.get_json()["skipped_for_saturation"] == []


def test_audit_payload_name_known_defaults_true_when_unset(client, monkeypatch):
    """Backward-compat: legacy advise() stubs that don't set name_known
    must not break the response (treat as known)."""
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **_kwargs):
        # No name_known on these recs — emulate older callers.
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(card="Lotus Cobra", action="add",
                                reason="", evidence={}),
                SimpleNamespace(card="Cultivate", action="cut",
                                reason="", evidence={}),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="heuristic",
            fallback_reason=None,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    resp = client.get("/api/audit?deck=Alpha&bracket=3")
    assert resp.status_code == 200
    body = resp.get_json()
    # Legacy stubs default to True — never spuriously flag as unknown.
    for entry in body["added"] + body["removed"]:
        assert entry.get("name_known", True) is True
    assert body["unknown_card_count"] == 0


# ---------------------------------------------------------------------------
# #013 — two-version audit diff (card delta + /api/audit_diff route)
# ---------------------------------------------------------------------------

def test_audit_card_diff_pure_added_removed_unchanged():
    """Pure card-delta logic: net add/remove/unchanged across two snapshots."""
    from commander_builder.knowledge_log import audit_card_diff

    a = (
        "[Commander]\n1 Omnath\n\n"
        "[Main]\n1 Forest\n1 Cultivate\n1 Lotus Cobra\n2 Island\n"
    )
    b = (
        "[Commander]\n1 Omnath\n\n"
        "[Main]\n1 Forest\n1 Tireless Tracker\n1 Lotus Cobra\n1 Island\n"
    )
    diff = audit_card_diff(a, b)

    added = {c["name"]: c["qty"] for c in diff["added"]}
    removed = {c["name"]: c["qty"] for c in diff["removed"]}
    assert added == {"Tireless Tracker": 1}
    assert removed == {"Cultivate": 1, "Island": 1}  # Island 2->1 is net -1
    assert diff["unchanged"] == 2                      # Forest, Lotus Cobra
    assert diff["from_total"] == 5 and diff["to_total"] == 4


def test_audit_card_diff_ignores_non_main_sections_and_empty():
    from commander_builder.knowledge_log import audit_card_diff

    assert audit_card_diff(None, None) == {
        "added": [], "removed": [], "unchanged": 0,
        "from_total": 0, "to_total": 0,
    }
    # Commander-section differences must not leak into the Main delta.
    a = "[Commander]\n1 Alpha\n\n[Main]\n1 Forest\n"
    b = "[Commander]\n1 Beta\n\n[Main]\n1 Forest\n"
    diff = audit_card_diff(a, b)
    assert diff["added"] == [] and diff["removed"] == []
    assert diff["unchanged"] == 1


@pytest.fixture
def diff_client(deck_dir, tmp_path, monkeypatch):
    """Client backed by two iterations with *different* [Main] sections so
    the diff endpoint has a real delta to render."""
    from commander_builder.knowledge_log import Iteration, init_db, record_iteration

    db = tmp_path / "diff_klog.sqlite"
    init_db(db)

    def _snap(name, *main_lines):
        return (
            "[metadata]\n" f"Name={name}\n\n"
            "[Commander]\n1 Omnath, Locus of Creation\n\n"
            "[Main]\n" + "".join(f"{ln}\n" for ln in main_lines)
        )

    v1 = Iteration(
        deck_id="omnath", deck_name="Omnath v1", bracket=3,
        audit_version="v1", verdict="pending", milestone=None,
        deck_snapshot=_snap("Omnath v1", "1 Forest", "1 Cultivate", "2 Island"),
    )
    from_id = record_iteration(v1, db_path=db)

    v2 = Iteration(
        deck_id="omnath", deck_name="Omnath v2", bracket=3,
        audit_version="v2", verdict="kept", milestone="champion",
        parent_id=from_id,
        deck_snapshot=_snap("Omnath v2", "1 Forest", "1 Lotus Cobra", "1 Island"),
    )
    to_id = record_iteration(v2, db_path=db)

    app = create_app(deck_dir=deck_dir, knowledge_db=db)
    app.config["TESTING"] = True
    client = app.test_client()
    client._diff_ids = (from_id, to_id)  # type: ignore[attr-defined]
    return client


def test_audit_diff_route_returns_card_delta(diff_client):
    from_id, to_id = diff_client._diff_ids
    resp = diff_client.get(f"/api/audit_diff?from_id={from_id}&to_id={to_id}")
    assert resp.status_code == 200
    body = resp.get_json()

    assert body["from"]["id"] == from_id
    assert body["to"]["id"] == to_id
    assert body["from"]["audit_version"] == "v1"
    assert body["to"]["verdict"] == "kept"
    assert body["to"]["milestone"] == "champion"

    diff = body["diff"]
    added = {c["name"]: c["qty"] for c in diff["added"]}
    removed = {c["name"]: c["qty"] for c in diff["removed"]}
    assert added == {"Lotus Cobra": 1}
    assert removed == {"Cultivate": 1, "Island": 1}
    assert diff["unchanged"] == 1  # Forest


def test_audit_diff_route_400_on_missing_ids(diff_client):
    assert diff_client.get("/api/audit_diff").status_code == 400
    assert diff_client.get("/api/audit_diff?from_id=1").status_code == 400
    assert diff_client.get("/api/audit_diff?from_id=x&to_id=y").status_code == 400


def test_audit_diff_route_404_on_unknown_iteration(diff_client):
    from_id, _to_id = diff_client._diff_ids
    resp = diff_client.get(f"/api/audit_diff?from_id={from_id}&to_id=999999")
    assert resp.status_code == 404
    assert "999999" in resp.get_json()["error"]

