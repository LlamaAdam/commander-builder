"""External credentials loader for commander_builder.

Reads `KEY=VALUE` pairs from a file LIVING OUTSIDE THE REPO so that
secrets like ANTHROPIC_API_KEY can never be committed by accident.
Default location is ``~/.commander-builder/credentials`` (a dot-dir in
the user's home directory, matching the convention git/ssh/npm/claude
itself all use). Override via the ``COMMANDER_BUILDER_CREDENTIALS``
environment variable for tests, CI, or non-standard setups.

Precedence (highest wins) -- never silently overwrite a real env var:

    shell environment > credentials file > nothing

This means a CI/container deployment that ships ``ANTHROPIC_API_KEY``
via secrets-manager env vars still works untouched; the file is a
local-dev convenience, not a production replacement.

File format (intentionally simple -- no escaping, no quoting):

    # Lines starting with '#' are comments. Blank lines ignored.
    ANTHROPIC_API_KEY=sk-ant-...
    MOXFIELD_USER=YourMoxfieldHandle

Permissions guidance (enforced advisory -- we WARN, we don't fix):

    Unix:    chmod 600 ~/.commander-builder/credentials
    Windows: right-click → Properties → Security → restrict to your user

The loader scans the file once per process. Call ``load_credentials()``
near the top of any CLI entry point that needs secrets. Calling it
again is cheap (no-op on subsequent calls).
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Optional


# Override path via env var. Useful for tests, CI, and users who want
# a non-standard location (e.g. mounting a secrets directory in a
# container). When set, the default ``~/.commander-builder/credentials``
# is NOT consulted -- explicit override beats default.
_OVERRIDE_ENV_VAR = "COMMANDER_BUILDER_CREDENTIALS"

# Default location: ``~/.commander-builder/credentials``. Works on
# Windows (where Path.home() is %USERPROFILE%), macOS, and Linux. We
# deliberately don't use $XDG_CONFIG_HOME -- the dot-dir-in-home
# convention is more universally understood and consistent across
# platforms. If someone wants the XDG path, they set the override env.
_DEFAULT_DIR = ".commander-builder"
_DEFAULT_FILE = "credentials"

# Process-wide guard so the loader only does its work once. The env
# overlay it produces is idempotent, but re-parsing the file on every
# CLI call would waste a few milliseconds and clutter logs.
_loaded: bool = False


def credentials_path() -> Path:
    """Resolve the active credentials file path.

    Returns the override env var's value when set (caller is asserting
    they know where the file is); otherwise returns the default
    ``~/.commander-builder/credentials``. The file may or may not
    exist -- caller checks ``Path.exists()`` if they care.
    """
    override = os.environ.get(_OVERRIDE_ENV_VAR)
    if override:
        return Path(override)
    return Path.home() / _DEFAULT_DIR / _DEFAULT_FILE


def load_credentials(
    path: Optional[Path] = None,
    *,
    force: bool = False,
    quiet: bool = False,
) -> dict[str, str]:
    """Load secrets from the external credentials file into ``os.environ``.

    Only keys NOT already set in the environment are populated -- shell
    env always wins so CI / container secrets stay authoritative.
    Returns the dict of keys actually applied (file present + key
    was missing from env). Empty dict when no file exists or every
    key was already set.

    Args:
      path  -- explicit file path. None (default) → use credentials_path().
      force -- re-parse even if a prior call already ran. Tests use this.
      quiet -- suppress the "no credentials file found" stderr hint that
              first-run users see. Default False; pass True from
              automated contexts (CI, test runners).

    Safety: malformed lines (missing ``=``) are skipped with a warning
    on stderr. Keys with empty values are dropped (treats them as
    "not configured" rather than "set to empty string"). Permissions
    looser than 0o600 trigger an advisory warning on Unix only --
    Windows ACL checks aren't worth the complexity for a single-user
    tool.
    """
    global _loaded
    if _loaded and not force:
        return {}
    _loaded = True

    target = path or credentials_path()
    if not target.exists():
        if not quiet:
            print(
                f"[secrets] No credentials file at {target}. "
                f"Run `commander-config init` to create one, or set "
                f"ANTHROPIC_API_KEY in your shell environment directly.",
                file=sys.stderr,
            )
        return {}

    # Permissions advisory -- Unix only. Windows ACLs would require a
    # win32 dependency to inspect; instead the docs tell Windows users
    # how to restrict access via Properties → Security.
    if not sys.platform.startswith("win"):
        try:
            mode = target.stat().st_mode
            # 0o077 mask catches "anything readable by group or other."
            if mode & 0o077:
                print(
                    f"[secrets] WARNING: {target} is readable by other "
                    f"users (mode {oct(mode & 0o777)}). Run `chmod 600 "
                    f"{target}` to restrict.",
                    file=sys.stderr,
                )
        except OSError:
            pass  # stat failed -- not worth blocking the load

    applied: dict[str, str] = {}
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"[secrets] ERROR: could not read {target}: {exc}",
            file=sys.stderr,
        )
        return {}

    for line_no, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            print(
                f"[secrets] WARNING: {target}:{line_no} skipped -- "
                f"no '=' in line: {stripped!r}",
                file=sys.stderr,
            )
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        # Strip wrapping quotes if present (common copy-paste from
        # env-file examples that show values in quotes).
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if not value:
            # Treat empty values as "not configured" -- don't overwrite
            # a real env var with an empty string just because the file
            # mentioned the key.
            continue
        # Shell env wins — but only if it has a NON-EMPTY value. An
        # empty env var (KEY="" in shell, or a stale `set KEY=` on
        # Windows) is treated as "not configured" so the file can
        # contribute a real value. Without this, a single accidental
        # `set ANTHROPIC_API_KEY=` in a shell session would silently
        # break every commander-* command for that session even though
        # the credentials file has the right key.
        if os.environ.get(key):
            continue
        os.environ[key] = value
        applied[key] = value

    return applied


def write_credentials_template(path: Optional[Path] = None) -> Path:
    """Create the credentials directory + file with a commented
    template if it doesn't already exist. Returns the file path.

    Refuses to overwrite an existing file -- that would silently destroy
    keys the user has already set. Use a text editor for updates.

    Sets file mode to 0o600 on Unix (owner read/write only). Windows
    leaves default ACLs; users restrict via Properties → Security if
    they want stricter access.
    """
    target = path or credentials_path()
    if target.exists():
        raise FileExistsError(
            f"{target} already exists. Edit it directly to update keys."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    template = (
        "# commander-builder credentials\n"
        "# Lines starting with '#' are comments. Blank lines ignored.\n"
        "# This file lives OUTSIDE the git repository so it can never\n"
        "# be committed by accident. See docs/SECRETS.md for details.\n"
        "#\n"
        "# Anthropic API key -- required for commander-auto-curate and\n"
        "# the Claude analyst path. Get one at https://console.anthropic.com\n"
        "# ANTHROPIC_API_KEY=sk-ant-...\n"
        "#\n"
        "# Optional: Moxfield user handle for personalized imports.\n"
        "# MOXFIELD_USER=YourHandle\n"
    )
    target.write_text(template, encoding="utf-8")
    # 0o600 = owner read/write only. No-op on Windows (chmod ignores
    # most bits) but harmless.
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return target


def config_main(argv: Optional[list[str]] = None) -> int:
    """Entry point for ``commander-config`` -- inspect / scaffold the
    external credentials file.

    Subcommands:
      show  -- print the active path + which keys are configured
              (values are redacted; only key names + presence shown).
      init  -- create the directory + template file if missing.
      path  -- print only the path (useful for shell scripting).
    """
    import argparse

    p = argparse.ArgumentParser(
        prog="commander-config",
        description=(
            "Inspect or scaffold commander-builder's external credentials "
            "file. The file lives OUTSIDE the repo so secrets can never "
            "be committed by accident. See docs/SECRETS.md."
        ),
    )
    p.add_argument(
        "subcommand", choices=["show", "init", "path"],
        help="show (current state) / init (create template) / path (echo file path).",
    )
    args = p.parse_args(argv)

    target = credentials_path()

    if args.subcommand == "path":
        print(str(target))
        return 0

    if args.subcommand == "init":
        try:
            written = write_credentials_template(target)
        except FileExistsError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(f"Wrote template: {written}")
        print()
        print("Next steps:")
        print(f"  1. Open {written} in your editor")
        print( "  2. Uncomment ANTHROPIC_API_KEY= and paste your key")
        print( "  3. Save the file. commander-auto-curate will pick it up automatically.")
        if not sys.platform.startswith("win"):
            print()
            print("Recommended permissions (owner-only access):")
            print(f"  chmod 600 {written}")
        return 0

    # show
    print(f"Credentials path: {target}")
    if not target.exists():
        print("  (file does not exist -- run `commander-config init` to create it)")
        return 0
    # Re-load force=True so we report on the file's actual current state
    # even if a prior load already ran in this process.
    applied = load_credentials(path=target, force=True, quiet=True)
    # Surface BOTH keys currently in os.environ AND keys that the file
    # would have set (whether or not the env already had them).
    file_keys: set[str] = set()
    try:
        for raw in target.read_text(encoding="utf-8").splitlines():
            s = raw.strip()
            if s and not s.startswith("#") and "=" in s:
                k = s.split("=", 1)[0].strip()
                if k:
                    file_keys.add(k)
    except OSError:
        pass
    if not file_keys:
        print("  (file exists but has no key=value lines)")
        return 0
    print("Keys in file:")
    for k in sorted(file_keys):
        in_env = k in os.environ
        applied_now = k in applied
        if in_env and not applied_now:
            note = "set (shell env takes precedence)"
        elif applied_now:
            note = "set (loaded from file)"
        else:
            note = "set"
        print(f"  {k:30s} {note}")
    if not sys.platform.startswith("win"):
        try:
            mode = target.stat().st_mode & 0o777
            if mode & 0o077:
                print()
                print(f"WARNING: file mode is {oct(mode)} -- readable by "
                      f"others. Run: chmod 600 {target}")
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(config_main())
