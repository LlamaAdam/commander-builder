# Negative-mode audit — 2026-09-01

Repository: `C:\dev\commander_builder`, branch `feat/fp-019-primer-heuristics`.

This was a bounded, deliberately skeptical review after the approved primer
hardening. The original findings and line references below describe the
pre-fix state on September 1. The approved fixes were implemented on September
2, as summarized below. Reproductions used temporary deck libraries or injected
dependencies. No real decks, saved configuration, or raw primer harvests were
edited.

## Resolution — 2026-09-02

All six reported areas have implementation fixes and regression coverage:

1. The dashboard reuses the shared quantity-aware Commander validator and
   distinguishes legal, illegal, and unverified results. Malformed active-zone
   entries are violations rather than silently ignored lines. Rules details
   retain affected card names and data-age warnings; canonical Forge foil names
   share the same resolved evidence as the validator.
2. Import preserves section headings and offers optional commander/partner
   fields. Existing decks have a **Change commander** dialog. Existing copies
   move between command zone and mainboard without changing the total; new names
   add one copy with an explicit warning, never a silent compensating cut.
3. Printing and finish suffixes normalize per line. The supplied 100-card
   Ur-Dragon export is a regression fixture, including both Forest entries,
   foil/etched suffixes, apostrophes, and multipart collector numbers. Legacy
   decorated commander names also survive an unchanged GET-to-PUT edit without
   gaining a duplicate copy.
4. Malformed and empty imports fail before creating a deck file, with useful
   parser errors. Invalid commander selections likewise leave the file intact.
5. Browser and desktop share explicit argument → environment → saved setting
   → caller-default directory resolution. Their intentional unconfigured
   defaults remain distinct: the browser uses Forge, desktop uses Documents.
6. Tests isolate card caches, Forge paths, configuration, and locks; shared
   dashboard memos reset between tests. Lock-content and file-mode assertions
   work with Windows semantics. Playwright supports `CB_E2E_PYTHON` and uses the
   appropriate default interpreter name on Windows.

The full slow-test lane additionally exposed a legacy live-Claude check that
selected an installed CLI without explicit live-service consent. It failed
before a model request because the optional Anthropic SDK was absent; the other
4,582 tests passed. Live tests now require `--run-live` as well as any slow-lane
selection, with eight policy regressions. The optional live check remains
available with its provider and dependencies configured. This is test isolation,
not a claim that the live provider was verified.

Independent review also drove regression fixes for closing a commander dialog
during a save, restoring keyboard focus after dashboard refresh, late imports
overriding newer navigation, and stale simulation results after commander edits
or A→B→A deck navigation. Review corrections were reproduced with failing tests
before being fixed.

Manual browser verification used a disposable library on port 5200 with stubbed
card data. A sectioned foil/etched paste imported with its commander and a
100-card total; moving an existing commander retained all 100 cards and returned
the former commander to the mainboard. Adding an absent commander visibly warned
about the addition and 101-card total. Reloading preserved the change, the rules
dialog explained the size violation, and closing the commander dialog restored
focus to its button. This verifies UI behavior, not the stubbed card rulings.

Final automated verification on Windows with Python 3.14:

- Full regression lane, `python -m pytest --run-slow -q`: **4,590 passed,
  1 skipped** in **636.85 seconds**. The sole skip is the explicit live-service
  opt-in described above; no offline slow tests were excluded.
- Playwright browser suite: **26 passed** in **41.7 seconds**, including the
  complete supplied 100-card export and delayed-response/navigation regressions.
- The live-consent policy's focused run passed **34 tests** with **1 explicit
  live skip**; direct marker selection could not bypass consent. Both final
  reviewers approved it without blockers.
- Wheel build passed, and the commander editor, bundled primer data, templates,
  JavaScript, and stylesheet were confirmed in the package. Python compilation,
  JavaScript syntax checks, staged whitespace validation, and the staged secret
  scan passed. Independent Python/backend/frontend reviews approved the fixes.
- The Python run emitted 1,031 existing positional-`maxsplit` deprecation
  warnings from `corpus_themes.py`; these are not test failures.

The browser tests use stub card data and simulation reports. No live Forge
gameplay or successful model-service request is claimed. These results verify
the changed behavior and regression suite, not every external service or card
ruling.

## Recommended order

### 1. P1 — The dashboard can incorrectly claim every card is legal

`src/commander_builder/deck_dashboard.py:526` looks up the nonexistent
`doctor.BANNED_IN_COMMANDER` attribute. The empty fallback leaves `illegal`
empty, and line 566 consequently sets `all_legal=True`.
`src/commander_builder/web/static/app.js:3186` renders that as
“All cards legal in Commander.”

Evidence: the golden single-commander fixture includes 40 fictional card names.
The live dashboard displayed the green legality claim while showing 40 cards
unsupported by Forge. An isolated reproduction returned dashboard
`all_legal=True` versus validator `status=unverified`. Even an injected lookup
explicitly identifying one card as banned left the dashboard green while the
validator returned `illegal` with `BANNED_CARD`.

Recommendation: reuse the existing validator's legal/illegal/unverified status;
keep missing evidence distinct from confirmed legality. Add regressions for
unknown names, banned cards, color identity, and malformed decks. If a banner
only checks the ban list, label that narrower scope explicitly.

### 2. P1 — Common pasted section headings lose the commander

`src/commander_builder/web/deck_text_ops.py:66` does not preserve plain
`Commander` and `Mainboard` headings. This input produced zero commanders and
three mainboard cards:

```text
Commander
1 The Ur-Dragon

Mainboard
1 Sol Ring
1 Arcane Signet
```

The paste-import form in `web/templates/index.html:260` has no commander field.
The live Edit deck dialog exposes raw deck text, not a simple commander picker.

Recommendation: preserve explicit section headings and provide an accessible
commander selector for pasted lists and existing decks, including partner pairs.

### 3. P1 — Foil/etched printing metadata becomes part of card names

`src/commander_builder/import_formats.py:84` does not normalize trailing `*F*`
or `*E*`. In the user's export syntax, a line such as
`1 The Ur-Dragon (PF25) 15 *F*` retained the printing and finish suffix in the
card name. Mixed lists cleaned some undecorated lines but not decorated lines;
all-foil lists fell through to the plain parser.

Recommendation: normalize printing metadata per line, independently of overall
format detection. Use the supplied 100-line deck as a regression fixture and
assert commander, quantities, apostrophes, multi-part collector numbers, and
foil/etched variants survive correctly.

### 4. P2 — Invalid imports can create zero-card decks successfully

`src/commander_builder/web/routes_decks.py:499` writes normalized content without
requiring any cards. Temporary Flask probes returned HTTP 200 for both
`hello world` and header-only `Count,Name\n`; the latter created a metadata-only
file without a `[Main]` section.

Recommendation: validate parsed contents before writing, reject empty imports
with a useful error, and leave the deck library unchanged on failure.

### 5. P2 — Browser and desktop can open different deck libraries

`src/commander_builder/web/app.py:211` does not use the persisted deck-directory
setting that `src/commander_builder/desktop.py:306` honors. With the same injected
configuration, desktop resolved the configured directory while browser health
reported the default Forge directory.

Recommendation: share directory resolution, with documented precedence for
explicit command-line arguments, environment, saved settings, and defaults.

### 6. P2 — Tests depend on the machine and on whether the app is open

`tests/test_desktop.py:61` calls `launch()` without injecting its instance lock.
Holding a temporary lock to simulate an already-running app made the wiring
test fail with `SingleInstanceError`.

The ordinary full suite also exposed dependencies on the real Scryfall cache,
installed Forge corpus, corpus memoization, and POSIX permission assumptions on
Windows. These failure-producing modules were not modified by the primer work.

Recommendation: isolate cache, Forge paths, and locks in test fixtures; clear
shared memoized state between tests. Make permission assertions platform-aware.

## Original audit verification and limits — 2026-09-01

- Primer/adoption/legality/knowledge-base focused suite: **157 passed**.
- Import-focused baseline during the audit: **35 passed**, despite the import
  defects above; these are coverage gaps, not evidence that importing is sound.
- Ordinary full suite: **4,301 passed, 12 failed, 168 skipped**. The 12 failures
  were traced to the machine-dependent test problems described above.
- A nine-test diagnostic with isolated Oracle/Forge state: **9 passed**.
- Full isolated rerun: **4,317 passed, 168 skipped, 3 deselected** in 579 seconds.
  Its in-memory pytest fixture redirects Oracle cache and Forge installation paths to temporary
  directories and resets the dashboard corpus cache. Three Windows-specific
  tests are explicitly deselected: the two second-handle lock-file reads and
  the POSIX file-mode preservation assertion. This is diagnostic evidence, not
  a claim that the ordinary full-suite invocation is green.
- Python compilation and `git diff --check` passed.
- Offline CLI adoption reports rendered in JSON and human-readable form with
  correct quantity totals, advisory legality, and missing-data warnings.
- Live browser smoke: temporary app on port 5001 loaded the library/dashboard,
  completed a heuristic audit, opened the deck editor, and closed it without
  saving. No browser warning/error log entries were captured. Server logs did
  show upstream image rate limits and an EDHREC fallback; external integrations
  were therefore not all clean. The temporary server was stopped afterwards.
- No Forge gameplay simulation or paid model call was run for this text/metadata
  change. This audit does not certify every app feature or every card ruling.

## Primer changes included separately

Primer links are references rather than automatic cut protection; explicit
`Protect=` metadata still pins cards. Win-line extraction now respects current
sections and excludes historical/TODO/cons text. Knowledge-base prompts separate
card-presence evidence from rules verification, with corrected conditional
Hell's Bells engine descriptions. Adoption reports count quantities and expose
the existing rules validator as warning-only guidance. Raw research and Claude's
harvested notes remain unchanged.
