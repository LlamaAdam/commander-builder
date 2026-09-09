"""FP-018.1 — the primer module: Delta parsing, sidecar storage, prompt caps.

OFFLINE ONLY. The two REAL captures do the shape-pinning:

* ``fixtures/hazel_primer.md`` — a single string op, no attributes, no
  embeds (deck 24864897, fetched 2026-08-20);
* ``fixtures/archidekt_primer_delta_86888.json`` — the formatted case:
  59 ops with header/list/italic attributes, 19 card-link embeds and an
  image embed (deck 86888, FP-018.4 harvest, 2026-08-27).

Constructed Delta inputs below are RENDERER-UNIT INPUTS, marked as such
— they exercise op-walk edge cases (skip rules, dedupe) and are NOT API
captures; no test invents a new claimed-from-the-API shape.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from commander_builder import primer
from commander_builder.primer import (
    FREE_TEXT_PROMPT_CHAR_CAP,
    clip_for_prompt,
    parse_primer,
    primer_sidecar_path,
    quoted_win_lines,
    read_primer_card_links,
    read_primer_sidecar,
    render_quill_delta,
    write_primer_sidecar,
)

FIXTURES = Path(__file__).parent / "fixtures"
HAZEL = FIXTURES / "hazel_primer.md"
SISAY = FIXTURES / "archidekt_primer_delta_86888.json"


def _hazel_delta() -> str:
    """The verbatim ``description`` field from the Hazel capture."""
    text = HAZEL.read_text(encoding="utf-8")
    return next(l for l in text.splitlines() if l.startswith('{"ops"'))


def _sisay() -> dict:
    return json.loads(SISAY.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# parse_primer — the Delta branch, on the real captures
# --------------------------------------------------------------------------- #

def test_hazel_capture_renders_to_the_players_words():
    parsed = parse_primer(_hazel_delta())
    assert parsed.was_delta is True
    assert parsed.card_links == []  # single-op capture: no embeds
    assert "sacrifice theme" in parsed.text
    assert "Squirreled Away" in parsed.text
    # No JSON punctuation leaks into the render.
    assert '{"ops"' not in parsed.text


def test_sisay_capture_renders_formatted_delta_with_embeds():
    """The formatted real capture: attributed string ops concatenate,
    card-link embeds render as their exact names AND are collected,
    the image embed is skipped."""
    deck = _sisay()
    parsed = parse_primer(deck["description"])
    assert parsed.was_delta is True
    # Attributed text ops made it through (the header op's text).
    assert "Sisay, Onion Queen" in parsed.text
    # Card-link embeds render inline — the sentence is not left with holes.
    assert "Laboratory Maniac" in parsed.text
    # ...and are collected as EXACT names, deduped, order preserved.
    assert parsed.card_links[0] == "Laboratory Maniac"
    assert len(parsed.card_links) == len(set(parsed.card_links))
    assert set(parsed.card_links) <= {c["name"] for c in deck["cards"]}
    # The image embed is an URL payload, not text: it must not render.
    assert "pbs.twimg.com" not in parsed.text


def test_render_quill_delta_is_the_text_view():
    assert render_quill_delta(_hazel_delta()) == parse_primer(
        _hazel_delta()).text


# --------------------------------------------------------------------------- #
# parse_primer — the non-Delta (Moxfield / degraded) branch
# --------------------------------------------------------------------------- #

def test_moxfield_markdown_passes_through_verbatim():
    """The provenance branch, intentional: Moxfield descriptions are
    markdown/plain text and never Delta (harvest-pinned), so not-JSON
    passes through untouched with was_delta=False."""
    md = "# My deck\n\nThis is a **markdown** primer with [a link](x).\n"
    parsed = parse_primer(md)
    assert parsed.text == md
    assert parsed.was_delta is False
    assert parsed.card_links == []


def test_invalid_json_degrades_to_plain_text_never_raises():
    broken = '{"ops": [{"insert": "unterminated'
    assert parse_primer(broken).text == broken
    assert parse_primer(broken).was_delta is False


def test_valid_json_that_is_not_a_delta_is_refused(tmp_path):
    """Re-pinned 2026-09-03 (R3 F-14). This test used to assert the
    literal passed through VERBATIM — so ``[1, 2, 3]`` became a sidecar
    whose primer said ``[1, 2, 3]``. A JSON literal / alien object is not
    the author's words; it parses to the honest empty primer and never
    writes a file."""
    for weird in ('[1, 2, 3]', '42', 'null', '0', '{"not_ops": []}',
                  '{"ops": "nope"}'):
        parsed = parse_primer(weird)
        assert parsed.text == "" and parsed.card_links == [], weird
        assert parsed.was_delta is False
        assert write_primer_sidecar(_deck(tmp_path), weird) is None
    assert not list(tmp_path.glob("*.primer.md"))


def test_json_encoded_bare_string_is_decoded():
    # '"hello"' is valid JSON; the decoded form is the readable one.
    assert parse_primer('"hello primer"').text == "hello primer"


def test_empty_and_none_render_empty():
    assert parse_primer(None).text == ""
    assert parse_primer("").text == ""


def test_op_walk_skip_rules():
    """CONSTRUCTED renderer-unit input (not an API capture): non-dict
    ops, non-string non-card-link inserts and blank card-links are
    skipped; duplicate card-links dedupe but still render inline."""
    delta = json.dumps({"ops": [
        {"insert": "a "},
        "not an op",
        {"insert": {"image": "https://x/y.png"}},
        {"insert": {"card-link": "Sol Ring"}},
        {"insert": " b "},
        {"insert": {"card-link": "  "}},
        {"insert": {"card-link": "Sol Ring"}},
        {"insert": 7},
    ]})
    parsed = parse_primer(delta)
    assert parsed.text == "a Sol Ring b Sol Ring"
    assert parsed.card_links == ["Sol Ring"]


# --------------------------------------------------------------------------- #
# Sidecar storage
# --------------------------------------------------------------------------- #

def _deck(tmp_path: Path) -> Path:
    p = tmp_path / "[USER] Hazel [B3].dck"
    p.write_text("[metadata]\nName=[USER] Hazel [B3]\n[Main]\n1 Forest\n",
                 encoding="utf-8")
    return p


def test_sidecar_lands_beside_the_deck_with_the_same_stem(tmp_path):
    deck = _deck(tmp_path)
    expected = tmp_path / "[USER] Hazel [B3].primer.md"
    assert primer_sidecar_path(deck) == expected


def test_write_and_read_round_trip_the_hazel_primer(tmp_path):
    deck = _deck(tmp_path)
    out = write_primer_sidecar(deck, _hazel_delta())
    assert out is not None and out.exists()
    text = read_primer_sidecar(deck)
    assert text is not None and "sacrifice theme" in text
    assert read_primer_card_links(deck) == []


def test_sidecar_records_and_returns_card_links(tmp_path):
    deck = _deck(tmp_path)
    write_primer_sidecar(deck, _sisay()["description"])
    links = read_primer_card_links(deck)
    assert links and links[0] == "Laboratory Maniac"
    # The machine block never leaks into the TEXT read — prompts get the
    # author's words only.
    text = read_primer_sidecar(deck)
    assert "primer-card-links" not in text
    assert "Sisay" in text


def test_empty_render_never_writes_a_sidecar(tmp_path):
    """An empty .primer.md would claim 'this deck has a primer and it
    says nothing' — a different (and false) claim from 'no primer'."""
    deck = _deck(tmp_path)
    assert write_primer_sidecar(deck, None) is None
    assert write_primer_sidecar(deck, "") is None
    assert write_primer_sidecar(deck, '{"ops": []}') is None
    assert write_primer_sidecar(deck, '{"ops": [{"insert": "  \\n"}]}') is None
    assert not primer_sidecar_path(deck).exists()


def test_missing_sidecar_reads_as_no_primer(tmp_path):
    deck = _deck(tmp_path)
    assert read_primer_sidecar(deck) is None
    assert read_primer_card_links(deck) == []


def test_rewrite_keeps_deck_and_primer_in_sync(tmp_path):
    """Re-pull semantics: the importer overwrites a re-pulled deck in
    place, and the sidecar must follow rather than keep a stale primer."""
    deck = _deck(tmp_path)
    write_primer_sidecar(deck, '{"ops": [{"insert": "old words"}]}')
    write_primer_sidecar(deck, '{"ops": [{"insert": "new words"}]}')
    assert read_primer_sidecar(deck) == "new words"


# --------------------------------------------------------------------------- #
# Prompt clipping + win-line quoting
# --------------------------------------------------------------------------- #

def test_clip_for_prompt_marks_truncation_explicitly():
    long = "x" * (FREE_TEXT_PROMPT_CHAR_CAP + 500)
    clipped = clip_for_prompt(long)
    assert clipped.startswith("x" * 100)
    assert "…[TRUNCATED —" in clipped
    assert f"{FREE_TEXT_PROMPT_CHAR_CAP} of {len(long)} chars" in clipped


def test_clip_for_prompt_leaves_short_text_untouched():
    assert clip_for_prompt("short") == "short"
    # The whole Hazel primer fits — the cap was sized to keep it intact.
    hazel_text = parse_primer(_hazel_delta()).text
    assert clip_for_prompt(hazel_text) == hazel_text


def test_quoted_win_lines_quotes_the_authors_own_combo_paragraph():
    """The explanation QUOTES win lines verbatim instead of paraphrasing
    (paraphrased combo lines are where explainers invent card behavior).
    The Hazel capture's own 'A game winning combo is' paragraph is the
    real test vector."""
    text = parse_primer(_hazel_delta()).text
    quotes = quoted_win_lines(text)
    assert quotes, "the Hazel primer's win line was not found"
    joined = " ".join(quotes)
    assert "game winning combo" in joined
    # Verbatim: the author's typos survive, proof nothing paraphrased.
    assert "concodant crossroads" in joined


def test_quoted_win_lines_empty_for_no_primer_or_no_win_talk():
    assert quoted_win_lines(None) == []
    assert quoted_win_lines("just a nice casual deck about trees") == []


@pytest.mark.parametrize("n", [1, 2])
def test_quoted_win_lines_respects_the_limit(n):
    text = "we win here\n\nwe combo there\n\ninfinite squirrels\n\nplain"
    assert len(quoted_win_lines(text, limit=n)) == n


# --------------------------------------------------------------------------- #
# R3 F-07 / F-08 / F-14 (2026-09-03) — sidecar identity, overwrite rules,
# machine-block encoding
# --------------------------------------------------------------------------- #

def test_sidecar_header_records_the_source_and_readers_strip_it(tmp_path):
    from commander_builder.primer import sidecar_identity

    deck = _deck(tmp_path)
    write_primer_sidecar(deck, _hazel_delta(), source_id="archidekt:24864897")
    ident = sidecar_identity(deck)
    assert ident["source"] == "archidekt:24864897"
    assert len(ident["sha256"]) == 64
    text = read_primer_sidecar(deck)
    assert "primer-source" not in text and "sha256" not in text
    assert "sacrifice theme" in text


def test_same_source_refreshes_only_when_the_words_changed(tmp_path):
    """R3 F-08: the CHANGELOG's "refuse-clobber" meant naming; the file was
    overwritten on every re-pull and hand notes vanished silently. Now an
    unchanged upstream leaves the file alone (hand notes survive) and a
    changed one refreshes it — and the caller is told which."""
    from commander_builder.primer import store_primer_sidecar

    deck = _deck(tmp_path)
    first = store_primer_sidecar(deck, '{"ops": [{"insert": "old words"}]}',
                                 source_id="abc")
    assert first.action == "written"
    sc = primer_sidecar_path(deck)
    sc.write_text(sc.read_text(encoding="utf-8") + "\nMY NOTES\n",
                  encoding="utf-8")
    again = store_primer_sidecar(deck, '{"ops": [{"insert": "old words"}]}',
                                 source_id="abc")
    assert again.action == "unchanged"
    assert "MY NOTES" in read_primer_sidecar(deck)
    changed = store_primer_sidecar(deck, '{"ops": [{"insert": "new words"}]}',
                                   source_id="abc")
    assert changed.action == "refreshed"
    assert read_primer_sidecar(deck) == "new words"


def test_another_sources_sidecar_is_never_clobbered(tmp_path):
    """R3 F-07: delete deck A in the web UI, import deck B whose name
    sanitizes to the same stem — B must not inherit (or overwrite) A's
    primer. The write is refused with a reason; the readers report the
    mismatch against the deck's own id."""
    from commander_builder.primer import (
        sidecar_identity_warning, store_primer_sidecar,
    )

    deck = _deck(tmp_path)
    store_primer_sidecar(deck, '{"ops": [{"insert": "deck A primer"}]}',
                         source_id="archidekt:111")
    res = store_primer_sidecar(deck, '{"ops": [{"insert": "deck B primer"}]}',
                               source_id="zzz")
    assert res.action == "refused" and "archidekt:111" in res.reason
    assert read_primer_sidecar(deck) == "deck A primer"
    # The deck on disk is B (Moxfield=zzz): the sidecar does not belong.
    deck.write_text("[metadata]\nName=[USER] Hazel [B3]\nMoxfield=zzz\n"
                    "[Main]\n1 Sol Ring\n", encoding="utf-8")
    warn = sidecar_identity_warning(deck)
    assert warn and "archidekt:111" in warn and "'zzz'" in warn
    # A matching deck: no warning. A deck with no id at all: warned too.
    deck.write_text("[metadata]\nArchidekt=111\n[Main]\n1 Sol Ring\n",
                    encoding="utf-8")
    assert sidecar_identity_warning(deck) is None
    deck.write_text("[Main]\n1 Sol Ring\n", encoding="utf-8")
    assert "carries no Moxfield=" in sidecar_identity_warning(deck)


def test_pre_r3_sidecar_without_a_header_still_reads(tmp_path):
    from commander_builder.primer import sidecar_identity, sidecar_identity_warning

    deck = _deck(tmp_path)
    primer_sidecar_path(deck).write_text(
        "<!-- primer-card-links (exact names from the source's card-link "
        "embeds; FP-018.3 auto-protect input)\nSol Ring\n-->\n\nold style\n",
        encoding="utf-8")
    assert read_primer_sidecar(deck) == "old style"
    assert read_primer_card_links(deck) == ["Sol Ring"]
    assert sidecar_identity(deck) is None
    assert sidecar_identity_warning(deck) is None


def test_card_link_names_with_newlines_or_arrows_round_trip(tmp_path):
    """R3 F-14: a link carrying a newline or the literal ``-->`` used to
    break the block's regex (``['Sol', 'Ring']``). Links are JSON-encoded
    one per line now."""
    deck = _deck(tmp_path)
    ops = [{"insert": "text "}]
    for n in ("Sol\nRing", "-->", "Good Ramp"):
        ops += [{"insert": {"card-link": n}}, {"insert": " "}]
    write_primer_sidecar(deck, json.dumps({"ops": ops}))
    assert read_primer_card_links(deck) == ["Sol\nRing", "-->", "Good Ramp"]
    assert "primer-card-links" not in read_primer_sidecar(deck)


# --------------------------------------------------------------------------- #
# R3 F-12 (2026-09-03) — win lines: word-bounded, heading-aware
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("prose", [
    "The winds of change blow.", "Open a window.", "Twincast copies.",
    "Loophole in the rules.", "Windgrace's Judgment.", "Winter Orb locks.",
    "Growing threats.", "Darwin's Vault",
])
def test_quoted_win_lines_are_word_bounded(prose):
    assert quoted_win_lines(prose) == []


def test_quoted_win_lines_quote_the_paragraph_under_a_win_heading():
    """The real deep primers (PRIMER_CORPUS.md Appendix A) put the win
    line under a bare 'Win Conditions' heading; the old scan quoted the
    heading itself and skipped bodies with no keyword."""
    text = ("General Strategy\nWe ramp and draw.\n\n"
            "Win Conditions\nJolrael makes Cat tokens as Baba draws; "
            "Bogbeast pumps the team for lethal.\n\n"
            "Weak Points\nLifegain is brutal against this deck.\n\n"
            "MVP Cards\n")
    quotes = quoted_win_lines(text)
    assert quotes == ["Jolrael makes Cat tokens as Baba draws; Bogbeast "
                      "pumps the team for lethal."]


def test_quoted_win_lines_never_silence_prose_for_what_it_mentions():
    """Reconciled with PR #85's rewrite (PR-03): no word list turns an
    in-sentence 'maybeboard' into a heading, so a win line that mentions
    one is still quoted."""
    text = ("I keep Sol Ring in the maybeboard. We win with Kiki-Jiki and "
            "Zealous Conscripts.")
    assert quoted_win_lines(text) == [text]
