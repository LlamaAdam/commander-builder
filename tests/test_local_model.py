"""local_model.py tests — OFFLINE ONLY, no daemon, no network.

Every HTTP round trip is mocked at ``urllib.request.urlopen`` (the same
seam ``test_analyst`` / ``test_proposer`` use for the old Ollama paths),
so a machine with no Ollama installed runs this file identically to one
that has it.

What is pinned here, in the order the module's failure contract states
it:

  * preflight is ACTIONABLE — daemon down and model-not-pulled produce
    different messages, and the not-pulled one names the exact
    ``ollama pull`` command;
  * every per-call failure (transport, timeout, empty body, prose,
    truncated JSON, out-of-taxonomy answer) returns ``None`` and the
    caller lands on the deterministic classifier;
  * the flag being off means ZERO network calls;
  * the agreement harness's arithmetic.

Card fixtures come from ``tests/fixtures/real_oracles`` per the fixture
rule — no synthetic oracle text, and no new fixture entries (which would
also require new ``EXPECTED_ROLE`` rows in
``tests/test_real_oracle_fixture.py``).
"""
import json
import urllib.error

import pytest

from commander_builder import local_model as lm
from commander_builder import staples
from commander_builder.local_model import (
    LocalModelClient,
    LocalModelConfig,
    LocalModelUnavailable,
    archetype_for_deck,
    measure_agreement,
    role_agreement,
    role_for_card,
)
from tests.fixtures.real_oracles import ORACLES


# --- fakes -----------------------------------------------------------------

class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _tags_body(*models: str) -> bytes:
    return json.dumps({"models": [{"name": m} for m in models]}).encode("utf-8")


def _generate_body(text: str) -> bytes:
    return json.dumps({"response": text}).encode("utf-8")


class _FakeDaemon:
    """Scripted stand-in for ``urllib.request.urlopen``.

    ``tags`` is the /api/tags outcome (bytes, or an exception to raise);
    ``generate`` is a list of /api/generate outcomes consumed in order
    (the last one repeats, so a one-element list answers every call).
    """

    def __init__(self, tags=None, generate=None):
        self.tags = tags if tags is not None else _tags_body(lm.DEFAULT_MODEL)
        self.generate = list(generate or [])
        self.tag_calls = 0
        self.generate_calls = 0
        self.timeouts: list[float] = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.timeouts.append(timeout)
        if url.endswith("/api/tags"):
            self.tag_calls += 1
            return self._emit(self.tags)
        assert url.endswith("/api/generate"), url
        self.generate_calls += 1
        if not self.generate:
            raise AssertionError("unexpected /api/generate call")
        idx = min(self.generate_calls - 1, len(self.generate) - 1)
        return self._emit(self.generate[idx])

    @staticmethod
    def _emit(outcome):
        if isinstance(outcome, BaseException):
            raise outcome
        return _FakeResponse(outcome)


def _client(daemon, **cfg) -> LocalModelClient:
    cfg.setdefault("model", lm.DEFAULT_MODEL)
    return LocalModelClient(LocalModelConfig(**cfg), opener=daemon)


def _oracle(name: str) -> dict:
    o = ORACLES[name]
    return {"name": name, "oracle_text": o["oracle_text"], "type_line": o["type_line"]}


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    """The tier is opt-in; no test inherits an operator's real setting."""
    monkeypatch.delenv(lm.LOCAL_MODEL_ENV_VAR, raising=False)
    monkeypatch.delenv(lm.LOCAL_MODEL_NAME_ENV_VAR, raising=False)
    monkeypatch.delenv(lm.LOCAL_MODEL_URL_ENV_VAR, raising=False)


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    monkeypatch.setattr(lm.time, "sleep", lambda _s: None)


# --- flag / config ---------------------------------------------------------

def test_disabled_by_default(monkeypatch):
    assert lm.is_enabled() is False
    for value in ("1", "true", "YES"):
        monkeypatch.setenv(lm.LOCAL_MODEL_ENV_VAR, value)
        assert lm.is_enabled() is True
    monkeypatch.setenv(lm.LOCAL_MODEL_ENV_VAR, "0")
    assert lm.is_enabled() is False


def test_config_from_env_overrides_model_and_url(monkeypatch):
    monkeypatch.setenv(lm.LOCAL_MODEL_NAME_ENV_VAR, "qwen3:14b")
    monkeypatch.setenv(lm.LOCAL_MODEL_URL_ENV_VAR, "http://box:9999")
    config = LocalModelConfig.from_env()
    assert config.model == "qwen3:14b"
    assert config.generate_url == "http://box:9999/api/generate"
    assert config.tags_url == "http://box:9999/api/tags"


def test_config_accepts_a_full_generate_url():
    """A value carried over from ``ProposerConfig.ollama_url`` still works."""
    config = LocalModelConfig(base_url="http://localhost:11434/api/generate/")
    assert config.base_url == "http://localhost:11434"
    assert config.tags_url == "http://localhost:11434/api/tags"


def test_taxonomies_are_imported_not_copied():
    """A role the shipped classifier can return must be answerable."""
    for role, _patterns in staples._ROLE_PATTERNS_COMPILED:
        assert role in lm.ROLE_TAXONOMY
    for extra in ("threat", "other", "land_payoff", "win_condition"):
        assert extra in lm.ROLE_TAXONOMY
    assert set(lm.ARCHETYPE_TAXONOMY) == {
        "aggro", "midrange", "control", "combo", "stax",
    }


# --- preflight -------------------------------------------------------------

def test_preflight_daemon_down_is_actionable():
    daemon = _FakeDaemon(tags=urllib.error.URLError("connection refused"))
    with pytest.raises(LocalModelUnavailable) as exc:
        _client(daemon).preflight()
    message = str(exc.value)
    assert "not reachable" in message
    assert "ollama serve" in message
    assert lm.LOCAL_MODEL_ENV_VAR in message
    assert daemon.generate_calls == 0


def test_preflight_model_not_pulled_names_the_pull_command():
    daemon = _FakeDaemon(tags=_tags_body("qwen2.5-coder:7b", "gpt-oss:20b"))
    with pytest.raises(LocalModelUnavailable) as exc:
        _client(daemon, model="llama3.2:3b").preflight()
    message = str(exc.value)
    assert "ollama pull llama3.2:3b" in message
    assert "qwen2.5-coder:7b" in message  # says what IS pulled
    assert daemon.generate_calls == 0


def test_preflight_http_error_is_not_reported_as_a_missing_daemon():
    """HTTPError subclasses URLError; conflating them is the old bug."""
    daemon = _FakeDaemon(tags=urllib.error.HTTPError(
        "http://localhost:11434/api/tags", 404, "Not Found", {}, None,
    ))
    with pytest.raises(LocalModelUnavailable) as exc:
        _client(daemon).preflight()
    assert "answered HTTP 404" in str(exc.value)
    assert "not reachable" not in str(exc.value)


def test_preflight_accepts_untagged_model_name():
    daemon = _FakeDaemon(tags=_tags_body("llama3.2:latest"))
    _client(daemon, model="llama3.2").preflight()  # does not raise


def test_preflight_rejects_a_different_tag_of_the_same_model():
    """An explicit tag must match exactly — a silent quantization swap
    would make any agreement measurement unreproducible."""
    daemon = _FakeDaemon(tags=_tags_body("llama3.2:1b"))
    with pytest.raises(LocalModelUnavailable):
        _client(daemon, model="llama3.2:3b").preflight()


def test_preflight_result_is_cached_across_calls():
    daemon = _FakeDaemon(generate=[_generate_body('{"role": "ramp"}')])
    client = _client(daemon)
    assert client.run("role_tag", oracle_text="Add {G}.", type_line="Artifact") == "ramp"
    assert client.run("role_tag", oracle_text="Add {G}.", type_line="Artifact") == "ramp"
    assert daemon.tag_calls == 1
    assert daemon.generate_calls == 2


# --- per-call failures all return None -------------------------------------

@pytest.mark.parametrize("bad_response", [
    "I think this is a ramp card, honestly!",          # prose only
    '{"role": "ra',                                     # truncated
    "",                                                 # empty body
    "[]",                                               # not an object
])
def test_malformed_response_returns_none(bad_response):
    daemon = _FakeDaemon(generate=[_generate_body(bad_response)])
    client = _client(daemon)
    assert client.run("role_tag", oracle_text="x", type_line="Instant") is None
    assert client.failures == 1


def test_valid_json_outside_the_taxonomy_is_rejected():
    """A syntactically fine answer that invents a category is malformed,
    not a new role."""
    daemon = _FakeDaemon(generate=[_generate_body('{"role": "card advantage"}')])
    assert _client(daemon).run("role_tag", oracle_text="x") is None


def test_wrong_shape_is_rejected():
    daemon = _FakeDaemon(generate=[_generate_body('{"label": "ramp"}')])
    assert _client(daemon).run("role_tag", oracle_text="x") is None

    daemon = _FakeDaemon(generate=[_generate_body('{"role": ["ramp"]}')])
    assert _client(daemon).run("role_tag", oracle_text="x") is None


def test_timeout_retries_once_then_returns_none():
    daemon = _FakeDaemon(generate=[TimeoutError("read timed out")])
    client = _client(daemon)
    assert client.run("role_tag", oracle_text="x") is None
    assert daemon.generate_calls == 2  # one attempt + one retry


def test_transient_failure_then_success_is_answered():
    daemon = _FakeDaemon(generate=[
        urllib.error.HTTPError("u", 503, "Service Unavailable", {}, None),
        _generate_body('{"role": "draw"}'),
    ])
    assert _client(daemon).run("role_tag", oracle_text="x") == "draw"
    assert daemon.generate_calls == 2


def test_deterministic_http_error_is_not_retried():
    daemon = _FakeDaemon(generate=[
        urllib.error.HTTPError("u", 400, "Bad Request", {}, None),
    ])
    assert _client(daemon).run("role_tag", oracle_text="x") is None
    assert daemon.generate_calls == 1


def test_generate_uses_the_short_timeout_not_the_retired_600s():
    daemon = _FakeDaemon(generate=[_generate_body('{"role": "ramp"}')])
    _client(daemon).run("role_tag", oracle_text="x")
    assert daemon.timeouts[-1] == lm.DEFAULT_TIMEOUT_SEC


def test_request_carries_the_schema_and_the_supplied_oracle_text():
    """The prompt must CARRY the evidence — the whole point of A4."""
    seen = {}

    def capture(req, timeout=None):
        if req.full_url.endswith("/api/tags"):
            return _FakeResponse(_tags_body(lm.DEFAULT_MODEL))
        seen.update(json.loads(req.data))
        return _FakeResponse(_generate_body('{"role": "wipe"}'))

    card = _oracle("Wrath of God")
    assert _client(capture).run(
        "role_tag", oracle_text=card["oracle_text"], type_line=card["type_line"],
    ) == "wipe"
    assert card["oracle_text"] in seen["prompt"]
    assert card["type_line"] in seen["prompt"]
    assert seen["format"]["properties"]["role"]["enum"] == list(lm.ROLE_TAXONOMY)
    assert seen["stream"] is False


# --- routers: local first, deterministic always ----------------------------

def test_flag_off_makes_zero_network_calls():
    def explode(*a, **kw):
        raise AssertionError("network call with the flag off")

    card = _oracle("Three Visits")
    assert role_for_card(card["oracle_text"], card["type_line"]) == "ramp"
    assert archetype_for_deck("[Main]\n1 Sol Ring\n", lookup=lambda n: None) == "midrange"
    # And with an explicit client whose opener would blow up if touched:
    client = LocalModelClient(LocalModelConfig(), opener=explode)
    assert role_for_card(
        card["oracle_text"], card["type_line"], client=client,
    ) == "ramp"


def test_malformed_response_falls_back_to_the_deterministic_role():
    card = _oracle("Cyclonic Rift")
    daemon = _FakeDaemon(generate=[_generate_body("no json here")])
    got = role_for_card(
        card["oracle_text"], card["type_line"],
        client=_client(daemon), enabled=True,
    )
    assert got == staples.classify_role_extended(
        card["oracle_text"], card["type_line"],
    ) == "wipe"


def test_out_of_taxonomy_response_falls_back_to_the_deterministic_role():
    card = _oracle("Mystical Tutor")
    daemon = _FakeDaemon(generate=[_generate_body('{"role": "search"}')])
    assert role_for_card(
        card["oracle_text"], card["type_line"],
        client=_client(daemon), enabled=True,
    ) == "tutor"


def test_valid_response_is_used_verbatim():
    card = _oracle("Sylvan Library")
    daemon = _FakeDaemon(generate=[_generate_body('{"role": "  Draw \\n"}')])
    assert role_for_card(
        card["oracle_text"], card["type_line"],
        client=_client(daemon), enabled=True,
    ) == "draw"


def test_unavailable_daemon_degrades_with_one_warning(capsys):
    """A misconfigured OPTIONAL tier warns; it does not break the run."""
    card = _oracle("Wrath of God")
    daemon = _FakeDaemon(tags=urllib.error.URLError("refused"))
    assert role_for_card(
        card["oracle_text"], card["type_line"],
        client=_client(daemon), enabled=True,
    ) == "wipe"
    out = capsys.readouterr().out
    assert "ollama serve" in out


def test_archetype_prompt_supplies_signals_and_never_the_answer():
    seen = {}

    def capture(req, timeout=None):
        if req.full_url.endswith("/api/tags"):
            return _FakeResponse(_tags_body(lm.DEFAULT_MODEL))
        seen.update(json.loads(req.data))
        return _FakeResponse(_generate_body('{"archetype": "stax"}'))

    signals = {
        "game_ending_combos": 0, "tutors": 1, "stax_cards": 7,
        "stack_count": 2, "wipe_count": 1, "instant_share": 0.1,
        "creature_share": 0.2, "avg_cmc": 2.8, "tribal_type": None,
        "oracle_coverage": 0.99, "oracle_available": True,
        "label": "control",  # the deterministic answer — must NOT leak
    }
    got = _client(capture).run(
        "archetype_tag", signals=signals, deck_name="Prison Deck",
    )
    assert got == "stax"
    prompt = seen["prompt"]
    assert "resource-denial (stax) cards: 7" in prompt
    assert "dominant creature type: unknown" in prompt  # None != 0
    assert "control" not in prompt.split("Measured signals:")[1]


def test_archetype_out_of_taxonomy_falls_back_to_the_v2_classifier(tmp_path):
    deck_text = "[Commander]\n1 Krenko, Mob Boss\n[Main]\n1 Sol Ring\n"
    daemon = _FakeDaemon(generate=[_generate_body('{"archetype": "ramp"}')])
    got = archetype_for_deck(
        deck_text, deck_name="x", lookup=lambda n: None,
        client=_client(daemon), enabled=True,
    )
    assert got == "midrange"


def test_archetype_valid_response_is_used():
    daemon = _FakeDaemon(generate=[_generate_body('{"archetype": "combo"}')])
    assert archetype_for_deck(
        "[Main]\n1 Sol Ring\n", lookup=lambda n: None,
        client=_client(daemon), enabled=True,
    ) == "combo"


# --- agreement harness -----------------------------------------------------

def test_measure_agreement_arithmetic():
    items = ["a", "b", "c", "d", "e"]
    local = {"a": "ramp", "b": "draw", "c": None, "d": "wipe", "e": None}
    det = {"a": "ramp", "b": "removal", "c": "ramp", "d": "wipe", "e": "other"}
    report = measure_agreement(
        "role_tag", items,
        local_fn=lambda i: local[i],
        deterministic_fn=lambda i: det[i],
        label_fn=lambda i: i,
        model="fake:1b",
    )
    assert (report.total, report.answered, report.agreed) == (5, 3, 2)
    assert report.unanswered == 2
    assert report.coverage == pytest.approx(3 / 5)
    assert report.agreement == pytest.approx(2 / 3)
    assert report.disagreements == (("b", "draw", "removal"),)
    assert report.to_dict()["model"] == "fake:1b"
    assert "NOT accuracy" in report.render()


def test_agreement_rates_are_none_when_the_denominator_is_zero():
    empty = measure_agreement(
        "role_tag", [], local_fn=lambda i: None,
        deterministic_fn=lambda i: "other", label_fn=str,
    )
    assert empty.coverage is None and empty.agreement is None

    unanswered = measure_agreement(
        "role_tag", ["a"], local_fn=lambda i: None,
        deterministic_fn=lambda i: "other", label_fn=str,
    )
    assert unanswered.coverage == 0.0
    assert unanswered.agreement is None  # not 0.0 — nothing was measured


def test_disagreement_list_is_capped():
    report = measure_agreement(
        "role_tag", list(range(10)),
        local_fn=lambda i: "ramp",
        deterministic_fn=lambda i: "draw",
        label_fn=str,
        max_disagreements=3,
    )
    assert report.answered == 10 and report.agreed == 0
    assert len(report.disagreements) == 3


def test_role_agreement_over_real_oracle_fixtures():
    """End-to-end over the byte-exact fixture cards, daemon mocked: the
    local tier answers 'ramp' for everything, so agreement equals the
    share of fixture cards the deterministic classifier calls ramp."""
    cards = [_oracle(name) for name in sorted(ORACLES)]
    daemon = _FakeDaemon(generate=[_generate_body('{"role": "ramp"}')])
    report = role_agreement(cards, client=_client(daemon))
    expected_agreed = sum(
        1 for c in cards
        if staples.classify_role_extended(c["oracle_text"], c["type_line"]) == "ramp"
    )
    assert report.total == report.answered == len(cards)
    assert report.agreed == expected_agreed
    assert daemon.generate_calls == len(cards)
    assert daemon.tag_calls == 1
    assert report.model == lm.DEFAULT_MODEL


def test_role_agreement_counts_unanswered_items_separately():
    cards = [_oracle(n) for n in ("Wrath of God", "Three Visits")]
    daemon = _FakeDaemon(generate=[_generate_body("prose, no json")])
    report = role_agreement(cards, client=_client(daemon))
    assert (report.total, report.answered, report.agreed) == (2, 0, 0)
    assert report.agreement is None


# --- task registry ---------------------------------------------------------

def test_task_registry_shape():
    assert set(lm.task_names()) == {"role_tag", "archetype_tag"}
    for name in lm.task_names():
        task = lm.get_task(name)
        assert task.schema["additionalProperties"] is False
        assert task.validate({}) is None
    with pytest.raises(KeyError, match="unknown local-model task"):
        lm.get_task("verdict")


def test_unknown_task_never_touches_the_network():
    daemon = _FakeDaemon()
    with pytest.raises(KeyError):
        _client(daemon).run("propose_swaps")
    assert daemon.tag_calls == 0


# --- CLI -------------------------------------------------------------------

def test_cli_tasks_lists_the_registry_without_touching_the_network(
        monkeypatch, capsys):
    def explode(*a, **kw):
        raise AssertionError("`tasks` must not reach the daemon")
    monkeypatch.setattr("urllib.request.urlopen", explode)

    assert lm.main(["tasks"]) == 0
    out = capsys.readouterr().out
    assert "role_tag" in out and "archetype_tag" in out


def test_cli_preflight_reports_the_actionable_error(monkeypatch, capsys):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _FakeDaemon(tags=_tags_body("gpt-oss:20b")),
    )
    assert lm.main(["preflight", "--model", "llama3.2:3b"]) == 2
    assert "ollama pull llama3.2:3b" in capsys.readouterr().err


def test_cli_preflight_ok(monkeypatch, capsys):
    monkeypatch.setattr(
        "urllib.request.urlopen", _FakeDaemon(tags=_tags_body("qwen3:14b")),
    )
    assert lm.main(["--model", "qwen3:14b", "preflight"]) == 0
    assert "qwen3:14b is pulled" in capsys.readouterr().out


def test_cli_agreement_over_a_cards_file(monkeypatch, tmp_path, capsys):
    cards_file = tmp_path / "cards.json"
    cards_file.write_text(json.dumps({
        name: {
            "oracle_text": ORACLES[name]["oracle_text"],
            "type_line": ORACLES[name]["type_line"],
        }
        for name in ("Three Visits", "Wrath of God")
    }), encoding="utf-8")

    monkeypatch.setattr("urllib.request.urlopen", _FakeDaemon(
        generate=[_generate_body('{"role": "ramp"}')],
    ))
    assert lm.main([
        "agreement", "--task", "role", "--cards", str(cards_file), "--json",
    ]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["total"] == 2
    assert report["answered"] == 2
    assert report["agreed"] == 1                      # Three Visits only
    assert report["disagreements"] == [["Wrath of God", "ramp", "wipe"]]
