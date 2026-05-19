"""Demo failing test: typo in import -> orchestrator should `apply_diff`.

This test imports `crete_app` (typo) from commander_builder.web.app
inside the test body. The real name is `create_app`. Local model
should propose a 1-line diff to fix the spelling in THIS test file.

Delete this file once the orchestrator demo run is complete.
"""


def test_app_factory_callable():
    from commander_builder.web.app import crete_app  # typo: should be create_app
    assert callable(crete_app)
