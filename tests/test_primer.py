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


def test_valid_json_that_is_not_a_delta_stays_verbatim():
    for weird in ('[1, 2, 3]', '42', '{"not_ops": []}', '{"ops": "nope"}'):
        parsed = parse_primer(weird)
        assert parsed.text == weird, weird
        assert parsed.was_delta is False


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
    sidecar = write_primer_sidecar(deck, _sisay()["description"])
    links = read_primer_card_links(deck)
    assert links and links[0] == "Laboratory Maniac"
    assert sidecar is not None
    marker = sidecar.read_text(encoding="utf-8").splitlines()[0]
    assert "exact-name references" in marker
    assert "auto-protect" not in marker
    # The machine block never leaks into the TEXT read — prompts get the
    # author's words only.
    text = read_primer_sidecar(deck)
    assert "primer-card-links" not in text
    assert "Sisay" in text


def test_sidecar_reader_accepts_the_legacy_auto_protect_marker(tmp_path):
    deck = _deck(tmp_path)
    primer_sidecar_path(deck).write_text(
        "<!-- primer-card-links (exact names from the source's card-link "
        "embeds; FP-018.3 auto-protect input)\n"
        "Laboratory Maniac\n"
        "-->\n\n"
        "Primer words.\n",
        encoding="utf-8",
    )

    assert read_primer_card_links(deck) == ["Laboratory Maniac"]
    assert read_primer_sidecar(deck) == "Primer words."


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


def test_quoted_win_lines_ignores_moxfield_chrome_before_primer():
    text = (
        "Moxfield deck page\n"
        "Overview  Primer  Win Conditions  History\n\n"
        "Primer\n\n"
        "# Overview\n\nA patient dragon deck.\n\n"
        "# Win Conditions\n\nWe win by attacking with a lethal dragon army."
    )

    assert quoted_win_lines(text) == [
        "We win by attacking with a lethal dragon army."
    ]


def test_quoted_win_lines_ignores_noncurrent_sections():
    text = (
        "# TODO\n\nTest an infinite combo with Future Card.\n\n"
        "# Cons\n\nThis deck can struggle to win through fogs.\n\n"
        "# Changelog\n\nRemoved the old Alpha plus Beta combo.\n\n"
        "# Win Conditions\n\nWe win with the current Gamma plus Delta combo."
    )

    assert quoted_win_lines(text) == [
        "We win with the current Gamma plus Delta combo."
    ]


def test_quoted_win_lines_ignores_qualified_noncurrent_headings():
    text = (
        "Removed Sections\n\nAn old infinite loop lived here.\n\n"
        "Updates 07/03/23\n\nThis update removed a winning combo.\n\n"
        "Change Log - 2025\n\nBeta plus Gamma used to combo.\n\n"
        "Combos in the maybe-board:\n\nFuture Card goes infinite here.\n\n"
        "Win Conditions\n\nWe win with the current Dragon attack."
    )

    assert quoted_win_lines(text) == [
        "We win with the current Dragon attack."
    ]


def test_quoted_win_lines_keeps_nested_changelog_content_excluded():
    text = (
        "# Changelog\n"
        "## 2025\n"
        "Removed the old Alpha plus Beta combo.\n"
        "# How It Wins\n"
        "We win with the current Dragon attack."
    )

    assert quoted_win_lines(text) == [
        "We win with the current Dragon attack."
    ]


def test_quoted_win_lines_handles_heading_and_body_in_one_text_block():
    text = (
        "TODO:\n- Test an infinite combo with Future Card.\n\n"
        "Cons:\nThis deck struggles to win through fogs.\n\n"
        "How do we win the game?\nAttack with enough Dragons to win."
    )

    assert quoted_win_lines(text) == [
        "Attack with enough Dragons to win."
    ]


def test_quoted_win_lines_does_not_return_a_heading_as_part_of_the_quote():
    text = "How to Win\nWe combo Alpha and Beta, then attack."

    assert quoted_win_lines(text) == [
        "We combo Alpha and Beta, then attack."
    ]


@pytest.mark.parametrize("text", [
    "Our winner is chosen at random.",
    "We keep swinging with dragons.",
    "Twinblade Paladin is a useful threat.",
])
def test_quoted_win_lines_requires_whole_win_words(text):
    assert quoted_win_lines(text) == []


def test_quoted_win_lines_keeps_unheaded_win_paragraphs_as_fallback():
    text = "A quiet introduction.\n\nWe win by attacking with dragons."

    assert quoted_win_lines(text) == ["We win by attacking with dragons."]
