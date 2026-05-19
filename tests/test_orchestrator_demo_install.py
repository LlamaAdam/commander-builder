"""Demo failing test: missing package -> orchestrator should `install_package`.

This test imports `pandas` (not in commander-builder's dependencies)
at test-body time, so pytest collection succeeds and the failure
shows up as a normal test error rather than a collection abort.

The autonomous-fix loop should classify this as `install_package`,
run `pip install pandas`, and the test will then pass.

Delete this file once the orchestrator demo run is complete.
"""


def test_pandas_is_importable():
    import pandas  # not in pyproject.toml -- fix is `pip install pandas`
    assert pandas.__version__
