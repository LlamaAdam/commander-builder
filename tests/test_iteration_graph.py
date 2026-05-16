"""Tests for ``knowledge_log.iteration_graph_for_deck``.

This helper feeds the SVG graph view in the dashboard. Given a deck_id
it walks the iteration history and returns nodes + edges in the shape
the UI can render directly:

    {
      "nodes": [
        {"id": int, "iteration_n": int, "bracket": int,
         "verdict": str, "created_at": str, "card_count": int,
         "price_usd": float | None, "audit_version": str | None},
        ...
      ],
      "edges": [
        {"from_id": int, "to_id": int,
         "applied_adds": [str, ...],
         "applied_cuts": [str, ...],
         "rationale": str,
         "price_delta_usd": float | None,
         "bracket_delta": int},
        ...
      ]
    }

Critical invariants:
- Nodes are ordered by id (== chronological).
- Edges come from parent_id — every iteration with a parent contributes
  one edge. Iterations without a parent (chain roots) contribute no edge.
- card_count counts qty-prefix lines in [Main] only; commanders excluded.
- price_usd reads from audit_manifest.pricing.total_price_usd (the
  enrichment from commit 9838245), None when absent.
- price_delta_usd is the child's price minus the parent's. None if
  either side is missing pricing.
- audit_summary fields preserve the audit_manifest's added/removed lists
  but cap reasonable display lengths via the UI, not here — keep the
  helper exhaustive so future renderers don't re-parse the source.
"""
from __future__ import annotations

import pytest

from commander_builder.knowledge_log import (
    Iteration,
    iteration_graph_for_deck,
    record_iteration,
)


_DECK_TEXT = (
    "[metadata]\nName=Test\n[Commander]\n1 Atraxa, Praetors' Voice\n"
    "[Main]\n"
    "1 Sol Ring\n1 Arcane Signet\n1 Cultivate\n"
)


def _record_chain(db, deck_id, count, *, with_pricing=False):
    """Seed `count` iterations in a chain for `deck_id`. Returns the
    list of new iteration ids (oldest first). Each row's parent_id is
    set to the prior row so they form a contiguous v1→...→vN chain."""
    ids = []
    parent = None
    for i in range(count):
        manifest = {
            "added": [f"AddedCard{i}"],
            "removed": [f"RemovedCard{i}"],
            "rationale": f"iteration {i + 1}",
        }
        if with_pricing:
            # Pricing block follows the commit-9838245 enrichment shape.
            manifest["pricing"] = {
                "total_price_usd": 100.0 + i * 10.0,
                "captured_at": "2026-05-15T00:00:00Z",
            }
        it = Iteration(
            deck_id=deck_id,
            deck_name=deck_id,
            bracket=3,
            parent_id=parent,
            audit_version="v3" if i == 0 else "claude-auto",
            audit_manifest=manifest,
            verdict="kept" if i < count - 1 else "pending",
            deck_snapshot=_DECK_TEXT,
        )
        new_id = record_iteration(it, db_path=db)
        ids.append(new_id)
        parent = new_id
    return ids


# ---------------------------------------------------------------------------
# Empty / no-iterations cases
# ---------------------------------------------------------------------------

def test_returns_empty_graph_for_unknown_deck(tmp_path):
    """A deck_id with no iterations returns an empty graph (no
    crash, no None). The UI's null-guard then hides the panel."""
    db = tmp_path / "knowledge_log.sqlite"
    result = iteration_graph_for_deck("not-a-real-deck", db_path=db)
    assert result == {"nodes": [], "edges": []}


# ---------------------------------------------------------------------------
# Node shape
# ---------------------------------------------------------------------------

def test_returns_one_node_per_iteration_ordered_by_id(tmp_path):
    """Single-deck chain of 3 iterations → 3 nodes in id order. The
    iteration_n field is 1-indexed and tracks position in the chain."""
    db = tmp_path / "knowledge_log.sqlite"
    ids = _record_chain(db, "deck-1", 3)
    result = iteration_graph_for_deck("deck-1", db_path=db)

    assert len(result["nodes"]) == 3
    assert [n["id"] for n in result["nodes"]] == ids
    assert [n["iteration_n"] for n in result["nodes"]] == [1, 2, 3]


def test_nodes_carry_bracket_verdict_and_audit_version(tmp_path):
    """The UI renders bracket and verdict on each node — pin so a
    refactor doesn't drop those fields from the projection."""
    db = tmp_path / "knowledge_log.sqlite"
    _record_chain(db, "deck-2", 2)
    result = iteration_graph_for_deck("deck-2", db_path=db)

    n1, n2 = result["nodes"]
    assert n1["bracket"] == 3
    assert n1["verdict"] == "kept"
    assert n1["audit_version"] == "v3"
    assert n2["verdict"] == "pending"
    assert n2["audit_version"] == "claude-auto"


def test_node_card_count_counts_main_lines_only(tmp_path):
    """card_count walks [Main] and sums quantity prefixes. The
    Commander section is excluded — Commander stays at 1 across the
    chain, so adding it to every node would just be noise."""
    db = tmp_path / "knowledge_log.sqlite"
    it = Iteration(
        deck_id="deck-cc", deck_name="X", bracket=3,
        deck_snapshot=(
            "[Commander]\n1 Test Commander\n"
            "[Main]\n"
            "1 Sol Ring\n"
            "3 Forest\n"          # qty 3 counts as 3
            "1 Lightning Bolt\n"
        ),
        audit_manifest={"added": [], "removed": []},
    )
    record_iteration(it, db_path=tmp_path / "knowledge_log.sqlite")
    result = iteration_graph_for_deck(
        "deck-cc", db_path=tmp_path / "knowledge_log.sqlite",
    )
    assert result["nodes"][0]["card_count"] == 5  # 1 + 3 + 1


def test_node_price_usd_reads_from_audit_manifest_pricing(tmp_path):
    """The pricing enrichment (commit 9838245) carries
    audit_manifest.pricing.total_price_usd. Each node surfaces that
    so the graph can render a $-delta on each edge."""
    db = tmp_path / "knowledge_log.sqlite"
    _record_chain(db, "deck-priced", 3, with_pricing=True)
    result = iteration_graph_for_deck("deck-priced", db_path=db)
    prices = [n["price_usd"] for n in result["nodes"]]
    assert prices == [100.0, 110.0, 120.0]


def test_node_price_usd_is_null_when_no_pricing(tmp_path):
    """Legacy rows without the pricing enrichment land with
    price_usd = None — UI then hides the price pill on that node."""
    db = tmp_path / "knowledge_log.sqlite"
    _record_chain(db, "deck-unpriced", 2)
    result = iteration_graph_for_deck("deck-unpriced", db_path=db)
    assert all(n["price_usd"] is None for n in result["nodes"])


# ---------------------------------------------------------------------------
# Edge shape
# ---------------------------------------------------------------------------

def test_returns_one_edge_per_parent_child_pair(tmp_path):
    """Chain of N iterations → N-1 edges. The chain root contributes
    no edge (no parent)."""
    db = tmp_path / "knowledge_log.sqlite"
    ids = _record_chain(db, "deck-3", 4)
    result = iteration_graph_for_deck("deck-3", db_path=db)
    assert len(result["edges"]) == 3
    # Edges thread root → leaf in id order.
    assert [e["from_id"] for e in result["edges"]] == ids[:-1]
    assert [e["to_id"] for e in result["edges"]] == ids[1:]


def test_edge_carries_applied_adds_and_cuts_from_child_manifest(tmp_path):
    """The edge into iteration N carries N's audit_manifest's
    added/removed (the DIFF that produced N from its parent)."""
    db = tmp_path / "knowledge_log.sqlite"
    _record_chain(db, "deck-4", 3)
    result = iteration_graph_for_deck("deck-4", db_path=db)
    # 3 iterations → 2 edges: → iter2, → iter3
    edge_to_iter2 = result["edges"][0]
    assert edge_to_iter2["applied_adds"] == ["AddedCard1"]
    assert edge_to_iter2["applied_cuts"] == ["RemovedCard1"]
    assert edge_to_iter2["rationale"] == "iteration 2"

    edge_to_iter3 = result["edges"][1]
    assert edge_to_iter3["applied_adds"] == ["AddedCard2"]


def test_edge_price_delta_is_child_minus_parent(tmp_path):
    """The $ delta on each edge is child.price - parent.price. UI
    renders 'Δ +$10' / 'Δ -$5' from this; a positive number means
    the iteration got more expensive."""
    db = tmp_path / "knowledge_log.sqlite"
    _record_chain(db, "deck-5", 3, with_pricing=True)
    result = iteration_graph_for_deck("deck-5", db_path=db)
    deltas = [e["price_delta_usd"] for e in result["edges"]]
    assert deltas == [10.0, 10.0]


def test_edge_price_delta_is_null_when_either_side_missing_price(tmp_path):
    """If either node lacks pricing, the delta is undefined — surface
    None rather than calling 0 a "no change" (it isn't)."""
    db = tmp_path / "knowledge_log.sqlite"
    # First iteration has pricing, second does not.
    it1 = Iteration(
        deck_id="deck-mixed", deck_name="X", bracket=3,
        audit_manifest={
            "added": [], "removed": [],
            "pricing": {"total_price_usd": 100.0,
                        "captured_at": "2026-05-15T00:00:00Z"},
        },
        deck_snapshot=_DECK_TEXT, verdict="kept",
    )
    id1 = record_iteration(it1, db_path=tmp_path / "knowledge_log.sqlite")
    it2 = Iteration(
        deck_id="deck-mixed", deck_name="X", bracket=3,
        parent_id=id1,
        audit_manifest={"added": [], "removed": []},
        deck_snapshot=_DECK_TEXT, verdict="pending",
    )
    record_iteration(it2, db_path=tmp_path / "knowledge_log.sqlite")

    result = iteration_graph_for_deck(
        "deck-mixed", db_path=tmp_path / "knowledge_log.sqlite",
    )
    assert result["edges"][0]["price_delta_usd"] is None


def test_edge_bracket_delta_captures_bracket_change(tmp_path):
    """If a deck moves from B3 to B4 between iterations, the edge
    surfaces bracket_delta=+1 so the UI can render an arrow."""
    db = tmp_path / "knowledge_log.sqlite"
    it1 = Iteration(
        deck_id="deck-bd", deck_name="X", bracket=3,
        audit_manifest={"added": [], "removed": []},
        deck_snapshot=_DECK_TEXT,
    )
    id1 = record_iteration(it1, db_path=db)
    it2 = Iteration(
        deck_id="deck-bd", deck_name="X", bracket=4,
        parent_id=id1,
        audit_manifest={"added": [], "removed": []},
        deck_snapshot=_DECK_TEXT,
    )
    record_iteration(it2, db_path=db)

    result = iteration_graph_for_deck("deck-bd", db_path=db)
    assert result["edges"][0]["bracket_delta"] == 1


def test_edge_handles_iteration_with_no_audit_manifest(tmp_path):
    """An iteration row can land with audit_manifest=None (legacy
    rows pre-dating the manifest enrichment). The edge into it must
    not crash; adds/cuts default to empty lists."""
    db = tmp_path / "knowledge_log.sqlite"
    it1 = Iteration(
        deck_id="deck-legacy", deck_name="X", bracket=3,
        audit_manifest={"added": [], "removed": []},
        deck_snapshot=_DECK_TEXT,
    )
    id1 = record_iteration(it1, db_path=db)
    it2 = Iteration(
        deck_id="deck-legacy", deck_name="X", bracket=3,
        parent_id=id1,
        audit_manifest=None,  # legacy shape
        deck_snapshot=_DECK_TEXT,
    )
    record_iteration(it2, db_path=db)

    result = iteration_graph_for_deck("deck-legacy", db_path=db)
    assert result["edges"][0]["applied_adds"] == []
    assert result["edges"][0]["applied_cuts"] == []
    assert result["edges"][0]["rationale"] == ""


# ---------------------------------------------------------------------------
# Forked chains (rare — bracket revert + re-audit creates one)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# /api/iteration_graph Flask endpoint
# ---------------------------------------------------------------------------

flask = pytest.importorskip("flask")


def test_iteration_graph_endpoint_returns_graph(tmp_path):
    """End-to-end: seed 3 iterations, hit /api/iteration_graph, verify
    the JSON payload carries deck_id + nodes + edges in the same shape
    the library helper returns."""
    from commander_builder.web.app import create_app

    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    db = tmp_path / "knowledge_log.sqlite"
    _record_chain(db, "endpoint-deck", 3, with_pricing=True)

    app = create_app(deck_dir=deck_dir, knowledge_db=db)
    app.config["TESTING"] = True
    client = app.test_client()

    resp = client.get("/api/iteration_graph?deck=endpoint-deck")
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["deck_id"] == "endpoint-deck"
    assert len(body["nodes"]) == 3
    assert len(body["edges"]) == 2
    # Pricing delta surfaces on each edge.
    assert all(e["price_delta_usd"] == 10.0 for e in body["edges"])


def test_iteration_graph_endpoint_400_without_deck(tmp_path):
    """The endpoint requires ?deck=<id>. No id → 400 so the client can
    catch a missing-arg bug at the call site, not as an empty payload."""
    from commander_builder.web.app import create_app

    decks = tmp_path / "decks"
    decks.mkdir()
    app = create_app(deck_dir=decks)
    app.config["TESTING"] = True
    client = app.test_client()

    resp = client.get("/api/iteration_graph")
    assert resp.status_code == 400


def test_iteration_graph_endpoint_empty_payload_for_unknown_deck(tmp_path):
    """Unknown deck → 200 with empty nodes/edges. Lets the UI render
    'no iterations yet' rather than treating absence as an error."""
    from commander_builder.web.app import create_app

    decks = tmp_path / "decks"
    decks.mkdir()
    app = create_app(deck_dir=decks, knowledge_db=tmp_path / "kl.sqlite")
    app.config["TESTING"] = True
    client = app.test_client()

    resp = client.get("/api/iteration_graph?deck=unknown")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["nodes"] == []
    assert body["edges"] == []


def test_handles_iterations_without_parent_in_middle_of_chain(tmp_path):
    """If two chain roots exist for the same deck (e.g. someone
    reset the iteration log and started over) they both render as
    nodes; the orphan iteration has no incoming edge."""
    db = tmp_path / "knowledge_log.sqlite"
    # Root 1
    it_a = Iteration(
        deck_id="deck-fork", deck_name="X", bracket=3,
        audit_manifest={"added": [], "removed": []},
        deck_snapshot=_DECK_TEXT,
    )
    id_a = record_iteration(it_a, db_path=db)
    # Child of root 1
    it_a2 = Iteration(
        deck_id="deck-fork", deck_name="X", bracket=3,
        parent_id=id_a,
        audit_manifest={"added": [], "removed": []},
        deck_snapshot=_DECK_TEXT,
    )
    record_iteration(it_a2, db_path=db)
    # Root 2 (new chain)
    it_b = Iteration(
        deck_id="deck-fork", deck_name="X", bracket=4,
        audit_manifest={"added": [], "removed": []},
        deck_snapshot=_DECK_TEXT,
    )
    record_iteration(it_b, db_path=db)

    result = iteration_graph_for_deck("deck-fork", db_path=db)
    # 3 nodes total.
    assert len(result["nodes"]) == 3
    # Only 1 edge (the parent-child pair); the second root has no edge.
    assert len(result["edges"]) == 1
