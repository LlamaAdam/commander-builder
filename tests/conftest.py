"""Pytest config: ensure `src/` is on sys.path so `commander_builder.*` imports
without an editable install.

Also defends against the kind of bug we hit on 2026-05-15: a test that
exercises a CLI path producing knowledge_log side effects without
passing ``--db-path`` ends up writing rows into the production
repo-root ``knowledge_log.sqlite``. The autouse fixture below redirects
``knowledge_log.DEFAULT_DB_PATH`` to a per-test temp file so a
careless test can't leak into production state again. The patch is
effective because ``knowledge_log`` resolves ``db_path=None`` against
the module attribute at CALL time (``_resolve_db_path``) — consumers
must never freeze the constant via ``from ... import DEFAULT_DB_PATH``
at module level or use it as a def-time parameter default.

## Fast/slow lane split (Tier-3, 2026-05-19)

Tests tagged ``@pytest.mark.slow`` are skipped by default so the
inner-loop ``pytest`` run takes ~30s instead of ~3min. Run the full
suite via ``pytest --run-slow`` (or ``pytest -m "slow or not slow"``
if you prefer pure marker syntax). CI runs the offline integration lane via
``--run-slow``. Tests marked ``live`` additionally require ``--run-live``;
neither ``--run-slow`` nor a marker expression opts into external services.

Tag a test ``slow`` when it:
- exercises the full ``advise()`` pipeline (EDHREC fixtures, multi-
  source dispatch, role classification) — each costs ~3-15s.
- shells out to the auto-curate CLI through argparse + Anthropic
  stubs — each costs ~2-4s.
- otherwise dominates the ``--durations=20`` list with >1s runtime.

## Network block (audit open bug 3, 2026-09-09)

The suite is offline-only, but until 2026-09-09 nothing ENFORCED it:
an unpatched lookup path quietly went to Scryfall / EDHREC / WotC and,
on a machine where those hosts are unreachable, showed up as a 30-45 s
test rather than a failure (three ``test_deck_builder`` tests measured
at 34-44 s each). ``network_block`` below refuses every non-loopback
socket connect with a ``NetworkBlockedError`` that names the test and
the host, and fails the test at teardown if the attempt was swallowed
by a degrade-don't-die guard. Loopback stays open (Flask's test client
and the desktop tests bind real 127.0.0.1 servers). ``live`` tests run
with ``--run-live`` are exempt.
"""
import email.message
import io
import ipaddress
import socket
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def pytest_addoption(parser):
    """Add ``--run-slow`` so devs can opt into the long-running
    integration tests without having to remember the marker syntax."""
    parser.addoption(
        "--run-slow",
        action="store_true",
        default=False,
        help=(
            "Run tests marked @pytest.mark.slow (advisor + auto-curate "
            "integration). Off by default; the fast lane keeps inner-"
            "loop iteration under ~30s. CI runs with this flag set."
        ),
    )
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help=(
            "Allow tests marked @pytest.mark.live to contact real services "
            "and consume subscription/API usage. Combine with --run-slow "
            "for live tests that are also marked slow."
        ),
    )


_AUTO_SLOW_NAME_PREFIXES = (
    # Every test_auto_curate_main_* test exercises argparse + the full
    # curator pipeline (~1-4s each); ~27 tests collectively cost
    # 30-60s of the ~3min suite. Auto-mark them rather than decorating
    # individually so adding a new CLI test inherits the slow tag.
    "test_auto_curate_main_",
)


def pytest_collection_modifyitems(config, items):
    """Auto-tag known-slow test families, then skip ``slow`` tests
    unless ``--run-slow`` was passed.

    Auto-tagging runs before the skip pass so a name-prefixed test
    picks up the marker regardless of whether the author remembered
    to add ``@pytest.mark.slow``.

    Skipping is implemented as a collection modifier (not a ``-m``
    default) so the skip reason is visible in the report and so
    users can still override with their own ``-m`` expression when
    debugging a specific slow test (e.g.
    ``pytest -m slow tests/test_proposer.py``).
    """
    # Pass 1: auto-mark by name prefix.
    slow_marker = pytest.mark.slow
    for item in items:
        for prefix in _AUTO_SLOW_NAME_PREFIXES:
            if item.name.startswith(prefix):
                item.add_marker(slow_marker)
                break

    # Live services need explicit consent even when slow tests or custom
    # marker expressions are selected. Keep this before the early returns.
    if not config.getoption("--run-live"):
        skip_live = pytest.mark.skip(
            reason="live-service test requires explicit --run-live opt-in",
        )
        for item in items:
            if "live" in item.keywords:
                item.add_marker(skip_live)

    # Pass 2: skip slow unless opted in.
    if config.getoption("--run-slow"):
        return
    marker_expr = config.getoption("-m") or ""
    if "slow" in marker_expr:
        return
    skip_slow = pytest.mark.skip(
        reason="slow test skipped by default; pass --run-slow to include",
    )
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)


@pytest.fixture(autouse=True)
def _reset_process_memos():
    """Clear process-level memo caches between tests.

    ``game_changers.load_game_changers`` and ``combo_detection.load_combos``
    memoize per process (2026-07-25 optimization pass) so hot loops stop
    paying a disk read — or, on the broken-WotC-scrape path, a live HTTPS
    round-trip — per call. Tests monkeypatch ``_http_get_text`` /
    ``CACHE_PATH`` / ``COMBO_DATA_PATH`` per test and assert on fetch
    behavior, so a memo populated by one test must never leak into the
    next. The combos cache is keyed on (path, mtime, size) and would
    usually self-invalidate, but clearing both keeps the isolation rule
    uniform and obvious.
    """
    from commander_builder import combo_detection as _cd
    from commander_builder import game_changers as _gc
    from commander_builder import scryfall_client as _sc
    _gc.clear_memo()
    _sc.clear_lookup_memo()
    _cd._COMBOS_CACHE = None
    yield
    _gc.clear_memo()
    _sc.clear_lookup_memo()
    _cd._COMBOS_CACHE = None


@pytest.fixture(autouse=True)
def _isolate_knowledge_log_default_path(tmp_path, monkeypatch):
    """Point ``knowledge_log.DEFAULT_DB_PATH`` at a per-test temp file.

    Tests that explicitly pass ``--db-path <somewhere>`` are unaffected
    — the override is only consulted when a caller doesn't supply one.
    Belt-and-suspenders against tests leaking iteration rows into the
    production repo-root ``knowledge_log.sqlite``.

    This only works because ``knowledge_log`` functions default to
    ``db_path=None`` and resolve it against the module attribute at
    call time (``_resolve_db_path``). Before 2026-07-19 the constant
    was baked into def-time defaults (``db_path: Path =
    DEFAULT_DB_PATH``) and import-time copies (``from .knowledge_log
    import DEFAULT_DB_PATH`` in doctor/status/export/report/revert_to),
    so this patch was silently a no-op and e.g. ``run_doctor()`` still
    init_db'd the production file. ``test_doctor.py::
    test_run_doctor_does_not_touch_production_knowledge_log`` guards
    against regressing that.

    A targeted leak was caught on 2026-05-15 in
    ``test_auto_curate_main_writes_versioned_file_without_dry_run``,
    which ran the full auto-curate pipeline without passing
    ``--db-path``. Four rows landed in production state and surfaced
    during a live-server probe of the iteration-graph endpoint. The
    test now passes ``--no-log`` explicitly, but this autouse fixture
    prevents the next slip-up from polluting state again.
    """
    from commander_builder import knowledge_log as _kl
    monkeypatch.setattr(
        _kl, "DEFAULT_DB_PATH", tmp_path / "_isolated_knowledge_log.sqlite",
    )


@pytest.fixture(autouse=True)
def _isolate_card_score_flag(monkeypatch):
    """Strip ``COMMANDER_BUILDER_CARD_SCORE`` from the test environment.

    Same hazard class as the two isolation fixtures around this one:
    the FP-015 tier-3 workflow has the operator EXPORT the flag in
    their shell, and ``card_score.is_enabled()`` reads ``os.environ``
    at call time — so without this, running the suite from that shell
    would send every flag-sensitive test down the flag-ON path except
    the handful that ``delenv`` explicitly. The suite's baseline is
    flag-off (the shipped default).

    Tests that deliberately exercise the flag-on path still work:
    they ``monkeypatch.setenv(...)`` inside the test body (or a
    non-autouse fixture), which runs AFTER this autouse fixture's
    setup — pinned by
    ``test_card_score.py::test_setenv_in_a_test_still_beats_the_autouse_delenv``.

    ``COMMANDER_BUILDER_CORPUS_NORMS`` (corpus_themes' builder-steering
    flag) gets the identical treatment for the identical reason: the
    suite's baseline is flag-off, and an operator who exported the flag
    must not silently flip every deck-builder test onto the norms path.

    ``COMMANDER_BUILDER_DECK_JUDGE`` (FP-016, 2026-08-20) joins them with
    a sharper edge: the improve-loop tests drive ``run_improve_loop``
    with scripted round functions over deck paths that do not exist, and
    with the flag exported the default judge path would try to open them
    on every round. The loop swallows that (a judge failure must never
    sink a round) but the WARN line would land in tests asserting on
    captured output — a flag-on shell must not change what the suite
    prints.
    """
    monkeypatch.delenv("COMMANDER_BUILDER_CARD_SCORE", raising=False)
    monkeypatch.delenv("COMMANDER_BUILDER_CORPUS_NORMS", raising=False)
    monkeypatch.delenv("COMMANDER_BUILDER_DECK_JUDGE", raising=False)


@pytest.fixture(autouse=True)
def _isolate_collection_path(tmp_path, monkeypatch):
    """Point the card-collection file at a per-test (nonexistent) temp
    path so tests never read the developer's real
    ``~/.commander-builder/collection.txt``.

    Same hazard class as ``_isolate_knowledge_log_default_path`` above:
    the ownership filters are contractually INERT when no collection
    file exists, and most tests assert on that inert baseline. A
    developer who has registered a real collection would otherwise see
    advisor/proposer tests fail (or worse, pass for the wrong reason)
    because their personal card list leaked into the pipeline under
    test.

    Works because ``collection.collection_path()`` consults the
    ``COMMANDER_BUILDER_COLLECTION`` env var AT CALL TIME (the
    DEFAULT_DB_PATH lesson — no import-time path constants). Tests
    that want a real collection write to this tmp path (or set the
    env var themselves / pass an explicit ``path=``).
    """
    monkeypatch.setenv(
        "COMMANDER_BUILDER_COLLECTION",
        str(tmp_path / "_isolated_collection.txt"),
    )


# --- Network block (audit open bug 3, 2026-09-09) -----------------------

class NetworkBlockedError(RuntimeError):
    """Raised by ``network_block`` on any non-loopback socket connect.

    Deliberately NOT an ``OSError``: urllib only wraps OSError into
    ``URLError``, and the repo's retry helpers (edhrec's backoff loop,
    ``oracle_store._call_with_retry``) only retry URLError / OSError /
    HTTPException / TimeoutError — so this escapes every backoff loop
    instead of being slept on and reaches the test (or the teardown
    check below) on the first attempt.
    """


# urllib reads these at every ``urlopen`` (``getproxies_environment``).
# A dev box or sandbox that routes egress through a LOOPBACK proxy
# (``HTTPS_PROXY=http://127.0.0.1:<port>``) would otherwise tunnel a
# blocked request straight through the loopback allowance below.
_PROXY_ENV_VARS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
)

_CALL_REPORT = pytest.StashKey()


def _is_local_host(host) -> bool:
    """Loopback / unspecified hosts are local; everything else is not.

    ``0.0.0.0`` / ``::`` are bind-side wildcards (werkzeug resolves them
    via getaddrinfo when a test starts a server); ``localhost`` and the
    empty host are what the stdlib passes for a loopback bind/connect.
    """
    if host is None or isinstance(host, bytes):
        host = (host or b"").decode("ascii", "replace")
    if host in ("", "localhost", "0.0.0.0", "::"):
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False  # a hostname — never trusted without resolution


def _is_local_address(address) -> bool:
    if not isinstance(address, tuple):
        return True  # AF_UNIX path (str/bytes) — always local.
    return _is_local_host(address[0])


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """Stash the call-phase report so ``network_block``'s teardown can
    tell "passed while a blocked connect was swallowed" (must fail) from
    "already failed on the raise itself" (don't double-report)."""
    rep = yield
    if rep.when == "call":
        item.stash[_CALL_REPORT] = rep
    return rep


@pytest.fixture(autouse=True)
def network_block(request, monkeypatch):
    """Refuse every non-loopback socket connect for the duration of a test.

    Guards ``socket.socket.connect`` / ``connect_ex`` (the addresses
    ``socket.create_connection`` and therefore ``http.client`` hand
    over) and ``socket.getaddrinfo`` (the DNS step that precedes them,
    so the error names the HOSTNAME and no resolver round-trip is paid
    on a box whose DNS hangs). Loopback and AF_UNIX stay open.

    A blocked attempt raises ``NetworkBlockedError`` immediately AND is
    recorded; if the test then passes anyway — the raise was eaten by a
    ``except Exception`` degrade guard, which is exactly how the three
    30-45 s ``test_deck_builder`` cases hid for months — teardown fails
    the test naming every host it reached for. Fix the test at the
    boundary the module already exposes (a urllib patch, an injected
    client, or a cached fixture); never loosen this fixture.

    Tests marked ``live`` are exempt when ``--run-live`` was given.
    Yields a namespace (``attempts``: the recorded ``host:port`` list)
    so a test that deliberately provokes the block can assert on it and
    clear it before teardown — see ``tests/test_network_block.py``.
    In-process only by design: a subprocess a test spawns inherits the
    stripped proxy env but not the socket guard.
    """
    state = SimpleNamespace(attempts=[], nodeid=request.node.nodeid)
    if (request.node.get_closest_marker("live") is not None
            and request.config.getoption("--run-live")):
        yield state
        return

    def _refuse(host, port):
        if isinstance(host, bytes):
            host = host.decode("ascii", "replace")
        where = f"{host}:{port}" if port is not None else str(host)
        state.attempts.append(where)
        raise NetworkBlockedError(
            f"test {state.nodeid} attempted a network connection to {where} "
            f"— the suite is offline-only. Mock at the boundary the module "
            f"already exposes (a urllib patch, an injected client, or a "
            f"cached fixture), or mark the test `live` and run with "
            f"--run-live."
        )

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def guarded_connect(self, address):
        if not _is_local_address(address):
            _refuse(address[0], address[1])
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        if not _is_local_address(address):
            _refuse(address[0], address[1])
        return real_connect_ex(self, address)

    def guarded_getaddrinfo(host, port, *args, **kwargs):
        if not _is_local_host(host):
            _refuse(host, port)
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    for var in _PROXY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    yield state

    if state.attempts:
        rep = request.node.stash.get(_CALL_REPORT, None)
        if rep is None or rep.passed:
            pytest.fail(
                f"test {state.nodeid} passed but a code path it exercised "
                f"attempted {len(state.attempts)} network connection(s) "
                f"that were swallowed by a degrade guard: "
                f"{sorted(set(state.attempts))}. The suite is offline-only "
                f"— mock at the boundary the module already exposes.",
                pytrace=False,
            )


# --- Offline seams for the three upstream services -------------------------
#
# Opt-in companions to ``network_block``: a family of tests that
# exercises a pipeline end-to-end (dashboard, audit route, deck build)
# does not need the network — it needs the upstream to MISS instantly
# so the degrade paths run as they would offline. Each fixture patches
# the ONE function the module routes every request through — the same
# seam the module's own unit tests already patch — and zeroes the
# courtesy sleep that sits in front of it, so a 99-card deck costs
# nothing instead of 99 × 0.1 s. A test that wants a different answer
# from the upstream patches over these in its own body (test-level
# monkeypatch runs after fixture setup).

def _offline_404(url: str) -> urllib.error.HTTPError:
    """A deterministic 404: propagates through every retry helper
    without a backoff sleep (404 is never retried), and every client
    turns it into a clean miss (None / {} / bundled fallback)."""
    return urllib.error.HTTPError(
        url, 404, "offline test suite", email.message.Message(), io.BytesIO(b""),
    )


@pytest.fixture
def offline_scryfall(monkeypatch):
    """Every Scryfall fetch misses instantly; disk snapshots still hit."""
    from commander_builder import scryfall_client as _sc

    def miss(url):
        raise _offline_404(url)
    monkeypatch.setattr(_sc, "_http_get_json", miss)
    monkeypatch.setattr(_sc, "REQUEST_SLEEP_SEC", 0.0)


@pytest.fixture
def offline_edhrec(monkeypatch):
    """Every EDHREC fetch misses instantly (no page → None / {})."""
    from commander_builder import edhrec_client as _ec

    def miss(url):
        raise _offline_404(url)
    monkeypatch.setattr(_ec, "_http_get_text", miss)
    monkeypatch.setattr(_ec, "REQUEST_SLEEP_SEC", 0.0)


@pytest.fixture
def offline_game_changers(monkeypatch):
    """The WotC scrape fails like a dead network → bundled fallback."""
    from commander_builder import game_changers as _gc

    def down(url, timeout=20):
        raise urllib.error.URLError("offline test suite")
    monkeypatch.setattr(_gc, "_http_get_text", down)


@pytest.fixture
def offline_moxfield(monkeypatch):
    """Every Moxfield fetch misses instantly (404 → "deck absent")."""
    from commander_builder import moxfield_import as _mx

    def miss(url):
        raise _offline_404(url)
    monkeypatch.setattr(_mx, "_http_get_json", miss)

