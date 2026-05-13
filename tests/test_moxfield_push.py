"""moxfield_push offline helper tests."""
from pathlib import Path

import pytest

from commander_builder.moxfield_push import (
    _api_push,
    dck_to_textarea,
    parse_dck_lines,
    prepare_push,
    to_moxfield_line,
)


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def test_parse_dck_lines_basic_sections(tmp_path):
    p = _write(tmp_path, "x.dck", "\n".join([
        "[metadata]",
        "Name=Test",
        "[Commander]",
        "1 Atraxa, Praetors' Voice|CMM|1",
        "[Main]",
        "1 Sol Ring|CMM|2",
        "1 Forest|UNF|451",
    ]))
    sections = parse_dck_lines(p)
    assert sections["commander"] == ["1 Atraxa, Praetors' Voice|CMM|1"]
    assert sections["main"] == ["1 Sol Ring|CMM|2", "1 Forest|UNF|451"]
    assert "metadata" not in sections  # Metadata is intentionally dropped.


def test_parse_dck_lines_skips_blank_and_pre_section(tmp_path):
    p = _write(tmp_path, "x.dck", "\n".join([
        "garbage before sections",
        "",
        "[Main]",
        "1 Sol Ring",
    ]))
    sections = parse_dck_lines(p)
    assert sections == {"main": ["1 Sol Ring"]}


def test_parse_dck_lines_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse_dck_lines(tmp_path / "nope.dck")


def test_dck_to_textarea_orders_commander_then_main(tmp_path):
    p = _write(tmp_path, "x.dck", "\n".join([
        "[Main]",
        "1 Sol Ring",
        "[Commander]",
        "1 Atraxa, Praetors' Voice",
    ]))
    out = dck_to_textarea(p)
    lines = out.splitlines()
    assert lines == ["1 Atraxa, Praetors' Voice", "1 Sol Ring"]


def test_dck_to_textarea_includes_sideboard_and_considering(tmp_path):
    p = _write(tmp_path, "x.dck", "\n".join([
        "[Commander]",
        "1 Foo",
        "[Main]",
        "1 Bar",
        "[Sideboard]",
        "1 Baz",
        "[Considering]",
        "1 Quux",
    ]))
    out = dck_to_textarea(p)
    assert "1 Baz" in out
    assert "1 Quux" in out
    # Order: commander, main, sideboard, considering.
    idx = lambda s: out.index(s)
    assert idx("Foo") < idx("Bar") < idx("Baz") < idx("Quux")


def test_dck_to_textarea_handles_unknown_sections(tmp_path):
    p = _write(tmp_path, "x.dck", "\n".join([
        "[Main]",
        "1 Foo",
        "[Tokens]",
        "1 Treasure Token",
    ]))
    out = dck_to_textarea(p)
    # Unknown sections come last, but their cards are preserved.
    assert "Treasure Token" in out


def test_prepare_push_returns_blob_even_without_clipboard(tmp_path, monkeypatch, capsys):
    p = _write(tmp_path, "x.dck", "[Main]\n1 Foo")
    # Force no clipboard backend.
    monkeypatch.setattr("commander_builder.moxfield_push._copy_to_clipboard", lambda _: False)
    out = prepare_push(p, copy_to_clipboard=True)
    assert out == "1 Foo"
    captured = capsys.readouterr()
    # When clipboard fails it should fall back to stdout.
    assert "1 Foo" in captured.out


def test_api_push_is_unimplemented():
    with pytest.raises(NotImplementedError):
        _api_push("any-id", {"foo": "bar"})


# ---------------------------------------------------------------------------
# to_moxfield_line — rewrites Forge "1 Name|SET|CN" → Moxfield "1 Name (SET) CN"
# ---------------------------------------------------------------------------

def test_to_moxfield_line_converts_pipe_suffix_to_parens():
    assert to_moxfield_line("1 Arcane Signet|MIC|157") == "1 Arcane Signet (MIC) 157"


def test_to_moxfield_line_preserves_bare_card_lines():
    assert to_moxfield_line("1 Forest") == "1 Forest"
    assert to_moxfield_line("3 Forest") == "3 Forest"


def test_to_moxfield_line_handles_dfc_split_names():
    line = "1 Sephiroth, Fabled SOLDIER // Sephiroth, One-Winged Angel|FIN|115"
    assert to_moxfield_line(line) == (
        "1 Sephiroth, Fabled SOLDIER // Sephiroth, One-Winged Angel (FIN) 115"
    )


def test_to_moxfield_line_uppercases_set_code():
    # Forge sometimes writes set codes in mixed case; Moxfield expects upper.
    assert to_moxfield_line("1 Sol Ring|c17|223") == "1 Sol Ring (C17) 223"


def test_to_moxfield_line_passes_through_high_quantities():
    assert to_moxfield_line("20 Swamp|J25|89") == "20 Swamp (J25) 89"


def test_to_moxfield_line_handles_collector_with_letters():
    # Promo / variant collector numbers can include letters (e.g., 161p).
    assert to_moxfield_line("1 The Great Henge|PELD|161p") == (
        "1 The Great Henge (PELD) 161p"
    )


def test_to_moxfield_line_returns_unchanged_on_garbage():
    assert to_moxfield_line("# comment line") == "# comment line"


def test_dck_to_textarea_converts_pipe_format(tmp_path):
    """Whole-deck integration: every line in the output should be
    Moxfield-paste-compatible (no pipes)."""
    p = _write(tmp_path, "x.dck", "\n".join([
        "[Commander]",
        "1 Sephiroth, Fabled SOLDIER // Sephiroth, One-Winged Angel|FIN|115",
        "[Main]",
        "1 Arcane Signet|MIC|157",
        "20 Swamp|J25|89",
        "1 Forest",   # already bare
    ]))
    out = dck_to_textarea(p)
    assert "|" not in out, "Moxfield rejects pipe-suffixed lines"
    assert "1 Arcane Signet (MIC) 157" in out
    assert "20 Swamp (J25) 89" in out
    assert "1 Forest" in out
    assert "Sephiroth, Fabled SOLDIER // Sephiroth, One-Winged Angel (FIN) 115" in out
