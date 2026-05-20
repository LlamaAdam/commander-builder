"""Shipped data files (JSON / TOML / CSV / etc.).

This is a package only so ``[tool.setuptools.package-data]`` can
ship the JSON files alongside the Python modules. There's no
runtime code here — modules that need a data file load it via
``importlib.resources`` or a path computed off ``__file__``.

Currently:
  - ``oracle_diff_buckets.json`` — bucket rules for the oracle-text
    drift detector (#019 + #020). See ``commander_builder.oracle_diff
    .load_diff_buckets``.
"""
