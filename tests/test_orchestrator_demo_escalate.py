"""Demo failing test: complex semantic bug -> orchestrator should escalate.

This test asserts something that requires understanding the project's
business logic to fix correctly. The local model should NOT be
confident enough to propose a fix, so this should escalate to Claude
(tier 2) or to needs_human.md.

The assertion is intentionally vague — there's no single right fix.
A safe orchestrator should escalate rather than guess.

Delete this file once the orchestrator demo run is complete.
"""


def test_strategy_synthesis_returns_at_least_three_lines():
    # Imagine this asserts behavior of a not-yet-implemented function.
    # The orchestrator can't know what to write without architectural
    # context, so escalation is the correct outcome.
    from commander_builder.advisor import synthesize_strategy_lines

    result = synthesize_strategy_lines(commander="Atraxa, Praetors' Voice")
    assert isinstance(result, list)
    assert len(result) >= 3
    assert all(isinstance(line, str) and len(line) >= 20 for line in result)
