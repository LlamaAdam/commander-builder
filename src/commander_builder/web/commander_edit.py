"""Quantity-preserving command-zone edits, independent of Flask and I/O."""
from __future__ import annotations

from ..dck_utils import (
    CARD_LINE_RE, count_commander_cards, count_main_cards, parse_card_line,
    section_card_names,
)
from ..import_formats import normalize_card_line, normalize_dck_cards
from .deck_text_ops import _dck_name_key


def commander_names(commander: object, partner: object = "", *, required: bool = True) -> list[str]:
    """Validate user-entered names before any deck-text interpolation."""
    names = []
    for label, value in (("commander", commander), ("partner", partner)):
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ValueError(f"{label} must be a card name")
        if (any(ord(char) < 32 or ord(char) == 127 or char in "[]|" for char in value)
                or len(value.splitlines()) > 1):
            raise ValueError(f"{label} must be one card name, without section or printing syntax")
        value = value.strip()
        if value:
            # Older imports exposed printing/finish suffixes in editor values.
            # Match the normalization used for stored deck rows before both
            # duplicate-partner validation and existing-copy selection.
            value = parse_card_line(normalize_card_line(f"1 {value}", 1))[1]
        names.append(value)
    if not names[0]:
        if required or names[1]:
            raise ValueError("commander is required")
        return []
    if names[1] and _dck_name_key(names[0]) == _dck_name_key(names[1]):
        raise ValueError("partner must be a different card from commander")
    return [name for name in names if name]


def commander_summary(text: str) -> tuple[list[str], list[str]]:
    # Normalize only the parsed names: listing candidates remains tolerant of
    # unrelated malformed rows, which the mutation path will reject safely.
    def normalized_names(section: str) -> list[str]:
        return [parse_card_line(normalize_card_line(f"1 {name}", 1))[1]
                for name in section_card_names(text, section)]

    commanders = normalized_names("Commander")
    candidates: dict[str, str] = {}
    for name in commanders + normalized_names("Main"):
        candidates.setdefault(_dck_name_key(name), name)
    return commanders, sorted(candidates.values(), key=str.casefold)


def _section_span(lines: list[str], section: str) -> tuple[int, int] | None:
    headers = [i for i, line in enumerate(lines) if line.strip().casefold() == f"[{section}]"]
    if len(headers) > 1:
        raise ValueError(f"deck has multiple [{section}] sections; repair the deck text first")
    if not headers:
        return None
    start = headers[0]
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip().startswith("[") and lines[i].strip().endswith("]")), len(lines))
    return start, end


def _take_one(body: list[str], name: str) -> str | None:
    for index, raw in enumerate(body):
        parsed = parse_card_line(raw)
        if parsed and _dck_name_key(parsed[1]) == _dck_name_key(name):
            match = CARD_LINE_RE.fullmatch(raw.strip())
            suffix = f"{match.group(2).strip()}{match.group(3) or ''}"
            if parsed[0] == 1:
                del body[index]
            else:
                body[index] = f"{parsed[0] - 1} {suffix}"
            return f"1 {suffix}"
    return None


def change_commander_text(text: str, requested: list[str]) -> tuple[str, list[str]]:
    """Move existing copies, demote previous commanders, and report new additions.

    Card legality is advisory and handled by the caller: no automatic cuts,
    no guessed substitutions, and no discarded partner/printing quantities.
    """
    text = normalize_dck_cards(text, require_cards=True)
    lines = text.splitlines()
    main_span = _section_span(lines, "main")
    command_span = _section_span(lines, "commander")
    if main_span is None:
        raise ValueError("deck has no [Main] section")
    main_body = lines[main_span[0] + 1:main_span[1]]
    command_body = lines[command_span[0] + 1:command_span[1]] if command_span else []
    chosen = []
    added = []
    for name in requested:
        card = _take_one(command_body, name) or _take_one(main_body, name)
        if card is None:
            card = f"1 {name}"
            added.append(name)
        chosen.append(card)
    former = [line for line in command_body if parse_card_line(line)]
    comments = [line for line in command_body if not parse_card_line(line)]
    replacements = [(main_span[0] + 1, main_span[1], former + main_body)]
    if command_span:
        replacements.append((command_span[0] + 1, command_span[1], chosen + comments))
    for start, end, body in sorted(replacements, reverse=True):
        lines[start:end] = body
    if command_span is None:
        lines[main_span[0]:main_span[0]] = ["[Commander]", *chosen]
    result = "\n".join(lines) + "\n"
    before = count_main_cards(text) + count_commander_cards(text)
    after = count_main_cards(result) + count_commander_cards(result)
    if after != before + len(added):
        raise RuntimeError("commander change did not preserve card quantities")
    return result, added


def commander_warnings(text: str, added: list[str]) -> list[str]:
    """Use cached rules evidence only; never delay an edit on external services."""
    from ..deck_legality import validate_deck
    from ..scryfall_client import lookup_card

    warnings = [f"Added 1 {name}: it was not in the mainboard or command zone." for name in added]
    report = validate_deck(text, lookup=lambda name: lookup_card(name, cache_only=True))
    warnings.extend(finding.message for finding in report.violations)
    if report.unverified:
        warnings.append("Commander legality is not fully verified from cached card data. Review the dashboard before playing.")
    if report.data_warning:
        warnings.append(report.data_warning)
    return warnings
