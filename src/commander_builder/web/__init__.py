"""FP-006 Flask scaffold — UI feed for the deck dashboard.

Optional package. Imports lazy because Flask is an optional dep:

    pip install commander-builder[web]
    python -m commander_builder.web

The dashboard data shape comes from
``commander_builder.deck_dashboard.build_dashboard``; this package only
wraps it in HTTP routes + a placeholder root page.
"""
from __future__ import annotations

__all__ = ["create_app"]


def create_app(*args, **kwargs):
    from .app import create_app as _create_app
    return _create_app(*args, **kwargs)
