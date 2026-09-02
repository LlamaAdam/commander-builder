"""Regression checks for explicit consent before live-service tests run."""
from types import SimpleNamespace

import pytest

from tests.conftest import pytest_collection_modifyitems


class _Item:
    def __init__(self, name, *keywords):
        self.name = name
        self.keywords = dict.fromkeys(keywords, True)
        self.markers = []

    def add_marker(self, marker):
        self.markers.append(marker)
        self.keywords[marker.name] = True


def _collect(item, *, run_slow=False, run_live=False, marker_expr=""):
    options = {
        "--run-slow": run_slow,
        "--run-live": run_live,
        "-m": marker_expr,
    }
    pytest_collection_modifyitems(
        SimpleNamespace(getoption=options.__getitem__), [item],
    )
    return [m.kwargs["reason"] for m in item.markers if m.name == "skip"]


@pytest.mark.parametrize("run_slow,marker_expr", [
    (True, ""), (False, "slow"), (False, "slow or not slow"), (True, "live"),
])
def test_slow_or_marker_opt_in_never_enables_live_services(run_slow, marker_expr):
    item = _Item("test_remote_service", "slow", "live")

    reasons = _collect(item, run_slow=run_slow, marker_expr=marker_expr)

    assert any("--run-live" in reason for reason in reasons)


@pytest.mark.parametrize("run_slow,marker_expr", [(True, ""), (False, "slow")])
def test_explicit_live_and_slow_opt_in_allows_live_services(run_slow, marker_expr):
    item = _Item("test_remote_service", "slow", "live")

    assert _collect(
        item, run_live=True, run_slow=run_slow, marker_expr=marker_expr,
    ) == []


def test_offline_slow_tests_still_run_without_live_opt_in():
    item = _Item("test_auto_curate_main_stubbed")

    assert _collect(item, run_slow=True) == []
    assert "slow" in item.keywords


def test_live_opt_in_does_not_implicitly_enable_slow_lane():
    item = _Item("test_remote_service", "slow", "live")

    reasons = _collect(item, run_live=True)

    assert len(reasons) == 1
    assert "--run-slow" in reasons[0]
