# Secrets & credentials

This project is a **public GitHub repo**. Any secret committed to source
will land on github.com and stay there until you rotate it. This doc is
the one-stop guide to keeping your API keys out of the repo.

## TL;DR

```sh
# 0. (One-time, if you haven't already.) Install the package so the
#    commander-* shell commands are available. Editable install means
#    you don't need to reinstall after git pulls.
pip install -e .[claude]

# 1. Scaffold the credentials file (outside the repo).
commander-config init

# 2. Open the file your shell printed and paste your key after the
#    commented ANTHROPIC_API_KEY= line.

# 3. (Unix only) Restrict access to your user.
chmod 600 ~/.commander-builder/credentials

# 4. Run any commander-* command. The key is picked up automatically.
commander-auto-curate "vendor/forge/userdata/decks/commander/[USER] Goblin [B4].dck" --bracket 4
```

That's the whole flow. The credentials file lives **outside the repo
directory tree** so git can't see it even with the most paranoid `git
add -A`. Read on for the why and the edge cases.

### Haven't run `pip install -e .` yet?

Every `commander-*` command in this doc has a `python -m` equivalent
that works without installing the package. Useful for quick smoke
runs or when you don't want a global entry-point shim:

| Entry point              | Module-form equivalent                              |
|--------------------------|------------------------------------------------------|
| `commander-config init`  | `python -m commander_builder._secrets init`          |
| `commander-config show`  | `python -m commander_builder._secrets show`          |
| `commander-config path`  | `python -m commander_builder._secrets path`          |
| `commander-auto-curate`  | `python -m commander_builder.proposer ...`           |
| `commander-doctor`       | `python -m commander_builder.doctor ...`             |
| `commander-bulk-import`  | `python -m commander_builder.moxfield_import ...`    |

The credentials file path and behavior are identical either way --
both forms invoke the same `config_main()` function under the hood.
For day-to-day use, `pip install -e .` once and the shorter shell
form just works.

## Where the file lives

Default path:

| Platform           | Path                                                        |
|--------------------|-------------------------------------------------------------|
| Windows            | `%USERPROFILE%\.commander-builder\credentials`              |
| macOS / Linux      | `~/.commander-builder/credentials`                          |

This is the `dot-dir-in-home` convention — the same pattern `git`,
`ssh`, `npm`, and `claude` itself use. Files there:

- ✅ Live outside your project directories, so `git status` in any repo
  never sees them.
- ✅ Are user-scoped, so every project on your machine can share one
  credentials file.
- ✅ Don't require any third-party dependencies (`python-dotenv` etc.)
  to load — pure stdlib.

### Custom location

If you'd rather put the file somewhere else (mounted volume, password
manager export, etc.), set the environment variable:

```sh
# Unix
export COMMANDER_BUILDER_CREDENTIALS=/run/secrets/commander-builder

# Windows cmd
set COMMANDER_BUILDER_CREDENTIALS=D:\secrets\commander-builder

# PowerShell
$env:COMMANDER_BUILDER_CREDENTIALS = "D:\secrets\commander-builder"
```

The loader checks the env var first, then falls back to the default.

## File format

```ini
# Lines starting with '#' are comments. Blank lines ignored.
ANTHROPIC_API_KEY=sk-ant-api03-...
MOXFIELD_USER=YourMoxfieldHandle

# Wrapping quotes are stripped on read, so this also works:
# ANTHROPIC_API_KEY="sk-ant-api03-..."
```

Rules:

- One `KEY=VALUE` per line.
- Leading/trailing whitespace around both `KEY` and `VALUE` is trimmed.
- Single or double wrapping quotes on the value are stripped (handy for
  copy-paste from `.env` examples that show values quoted).
- `KEY=` with an empty value is treated as "not configured" — the
  loader won't overwrite a real env var with an empty string.
- Malformed lines (no `=`) are skipped with a warning on stderr.

## Precedence

The loader **never overwrites a value that's already in `os.environ`**.
Order of precedence (highest wins):

1. **Shell environment variable** (e.g. `export ANTHROPIC_API_KEY=...`
   in your shell profile, or a CI/container secret).
2. **External credentials file** (the workflow this doc describes).
3. **Nothing** — commands that need the key will exit with a clear
   error pointing back here.

This means:

- **Production deployments** using container-secrets or CI env vars
  continue to work untouched. The file is a local-dev convenience, not
  a production replacement.
- **Local dev** can keep the key in one place (`~/.commander-builder/
  credentials`) without remembering to export it in every new terminal.
- **Power users** can override the file's key for a single run by
  prepending the env var: `ANTHROPIC_API_KEY=sk-different commander-auto-curate ...`.

## CLI commands

```sh
# Print the active credentials path (useful for scripting).
commander-config path

# Inspect the file: which keys are set, where, and any permission
# warnings. Values are REDACTED — only key names are printed.
commander-config show

# Create the directory + a commented template at the default path.
# Refuses to overwrite if the file already exists.
commander-config init
```

## Why not just use `.env`?

The repo's `.gitignore` already excludes `.env` and `.env.local`, so
that pattern would also work. We prefer the external-file approach for
two reasons:

1. **Physical separation.** A `.env` lives inside the repo tree, which
   means `git add -A` from a wrong directory could theoretically stage
   it if `.gitignore` was deleted, mis-edited, or bypassed with
   `git add -f`. The external file is in your home directory — git
   doesn't know it exists.
2. **Cross-project sharing.** One `~/.commander-builder/credentials`
   file works for every commander-builder checkout you have. No
   per-clone `.env` to keep in sync.

The flip side: a `.env` is more discoverable for someone who's used
that pattern in other Python projects. If you want both, you can use a
shell-profile alias to `cp .env.example ~/.commander-builder/credentials`
on setup.

## Common operations

### Rotate the key

```sh
# Edit the file in your favorite editor.
$EDITOR $(commander-config path)
```

Replace the line with the new key, save, exit. The next command run
picks up the new value automatically — no reload step.

### Audit what's set

```sh
commander-config show
```

Outputs something like:

```
Credentials path: /home/alice/.commander-builder/credentials
Keys in file:
  ANTHROPIC_API_KEY              set (loaded from file)
  MOXFIELD_USER                  set (shell env takes precedence)
```

The "shell env takes precedence" note means a real env var is winning
over the file's value — useful for spotting setup confusion.

### Move to a new machine

```sh
# Old machine: copy the file (NOT the repo).
scp ~/.commander-builder/credentials newmachine:~/.commander-builder/credentials

# New machine: tighten permissions.
chmod 600 ~/.commander-builder/credentials
```

The repo travels via `git clone`; the credentials travel separately.
Exactly the same separation `ssh` keys have.

## Permissions

On Unix, the credentials file should be readable only by you:

```sh
chmod 700 ~/.commander-builder        # directory: owner-only
chmod 600 ~/.commander-builder/credentials  # file: owner read/write
```

`commander-config init` sets these for you. The loader warns on stderr
if it spots looser permissions:

```
[secrets] WARNING: /home/alice/.commander-builder/credentials is
readable by other users (mode 0o644). Run `chmod 600 ...` to restrict.
```

On Windows, NTFS ACLs are harder to inspect programmatically (would need
a win32 dependency). The loader skips the check on Windows. If you
share your machine, right-click → Properties → Security and restrict the
file to your user account manually.

## If you accidentally commit a key

It happens. Don't panic, but **act immediately**:

1. **Rotate the key first.** Go to
   [console.anthropic.com](https://console.anthropic.com) → Settings →
   API Keys, revoke the leaked key, generate a new one. The leaked
   key is compromised the moment it lands on a public repo, even if
   you delete the commit. Rotation is the only real fix.
2. Update `~/.commander-builder/credentials` with the new key.
3. Remove the key from git history. The simplest path:
   ```sh
   git rm <bad-file>
   git commit --amend --no-edit  # if it was the most recent commit
   git push --force-with-lease    # only if you JUST pushed and no one else has pulled
   ```
   For deeper history rewrites, use [`git filter-repo`](https://github.com/newren/git-filter-repo).
4. Audit recent commits for any other secrets. Grep for `sk-`, `Bearer`,
   `password=`, `token=`. The architecture doc (`docs/architecture.md`)
   suggests a pre-commit hook pattern.

## Production / CI

For deployments, set the env var directly via your platform's secret
mechanism — don't ship the credentials file:

- **GitHub Actions**: `secrets.ANTHROPIC_API_KEY` → workflow `env:` block.
- **Docker**: `--env-file` or `--env ANTHROPIC_API_KEY=...`.
- **Kubernetes**: a `Secret` resource mounted as env vars.
- **Cloud Run / Lambda / Fargate**: the platform's secrets manager.

The loader silently no-ops when the file is absent and the env var is
set, so the same code runs locally and in production with zero changes.
