"""_llm_json tests — the shared robust JSON extractor for LLM responses.

The recovery strategy itself (whole-text parse, fence strip, brace
counting) is also exercised through its original home's back-compat
surface in test_proposer_auto.py (`_extract_curator_json`). This file
pins the SHARED module's contract: the raising variant's error class,
message content (context + head/tail snippets), and the dict-only rule.
"""
import json

import pytest

from commander_builder._llm_json import (
    LLMJsonError,
    extract_json_object,
    try_extract_json_object,
)


# --- try_extract_json_object (Optional contract) ---------------------------

def test_plain_json_object():
    assert try_extract_json_object('{"a": 1}') == {"a": 1}


def test_fenced_json():
    raw = '```json\n{"a": 1}\n```'
    assert try_extract_json_object(raw) == {"a": 1}


def test_prose_before_fenced_json():
    """The shape the old startswith-``` strips missed: prose, THEN fence."""
    raw = 'Looking at this deck...\n```json\n{"a": 1}\n```'
    assert try_extract_json_object(raw) == {"a": 1}


def test_prose_before_and_after_bare_json():
    raw = 'Here you go:\n{"a": 1}\nLet me know if you need more.'
    assert try_extract_json_object(raw) == {"a": 1}


def test_braces_inside_strings_do_not_confuse_scanner():
    obj = {"rationale": "curly {braces} and \"quotes\" inside", "adds": []}
    raw = "Preamble.\n" + json.dumps(obj) + "\nTrailer."
    assert try_extract_json_object(raw) == obj


def test_truncated_json_returns_none():
    """max_tokens cutoff: the object never closes, nothing is parseable."""
    assert try_extract_json_object('{"adds": ["A", "B"], "cu') is None


def test_prose_only_returns_none():
    assert try_extract_json_object("No JSON anywhere in this response.") is None


def test_empty_and_none_return_none():
    assert try_extract_json_object("") is None
    assert try_extract_json_object("   \n  ") is None


def test_top_level_list_is_not_an_object():
    """Every LLM contract in this project is an object schema; a bare list
    must not slip through and crash the caller's .get() later."""
    assert try_extract_json_object('[1, 2, 3]') is None


def test_object_nested_inside_list_is_recovered():
    """A model that wraps the object in a stray array still yields the
    object via the brace scanner."""
    assert try_extract_json_object('[{"a": 1}]') == {"a": 1}


# --- extract_json_object (raising contract) --------------------------------

def test_extract_returns_dict_on_success():
    assert extract_json_object('{"a": 1}') == {"a": 1}


def test_extract_raises_llm_json_error_with_context():
    with pytest.raises(LLMJsonError, match="my_call_site"):
        extract_json_object("just prose", context="my_call_site")


def test_llm_json_error_is_a_value_error():
    """Subclass relationship is part of the contract: generic ValueError
    handlers keep working, but routers can single out LLMJsonError to
    tell 'garbage response' from 'backend unavailable'."""
    assert issubclass(LLMJsonError, ValueError)


def test_extract_error_quotes_head_of_short_response():
    with pytest.raises(LLMJsonError, match="totally not json"):
        extract_json_object("totally not json", context="ctx")


def test_extract_error_quotes_head_and_tail_of_long_response():
    """Long responses get head AND tail snippets: the head shows prose,
    the tail is the tell for max_tokens truncation."""
    long_garbage = "HEADMARK " + ("filler " * 200) + '{"cut": "TAILMARK'
    with pytest.raises(LLMJsonError) as exc_info:
        extract_json_object(long_garbage, context="ctx")
    msg = str(exc_info.value)
    assert "HEADMARK" in msg
    assert "TAILMARK" in msg
    # The full multi-KB response must NOT be dumped into the log line.
    assert len(msg) < len(long_garbage)
