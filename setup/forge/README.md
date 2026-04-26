# Forge install assets

Files used by the Phase 1A unattended install of Forge into `vendor/forge/`.
These are the artifacts that took several iterations to figure out — keep
them in git so future setups skip the discovery work.

## Files

| File | Purpose |
|------|---------|
| `auto-install.xml` | IzPack 5 unattended-install record. Drives the Forge installer's panels (HTMLInfo → Target → Packs → Install → Finish) without user input. |
| `logging.properties` | `java.util.logging` config that surfaces IzPack's silent failures. Without this the installer prints only `[ Automated installation FAILED! ]` on error — with no stack trace. |
| `forge.profile.properties` | Tells Forge to keep its userdata under `vendor/forge/userdata/` instead of `%APPDATA%/Forge/`. Required because the headless `sim` mode reads decks from `<userDir>/decks/<format>/` and ignores the documented `-D` flag. |

## How they were derived

- **Panel order + IDs**: extracted from the binary `resources/panelsOrder` Java-serialized blob inside `forge-installer-2.0.12.jar`. IDs are `welcome`, `install_dir`, `sdk_pack_select`, `install`, `finish`.
- **Pack names**: extracted from `resources/packs/pack-*` listings in the same jar (`Forge pack`, `Script pack`).
- **`-D` flag is broken**: `forge.exe sim -h` advertises `-D <directory>` but in 2.0.12 it is silently ignored. Forge always reads from `<userDir>/decks/<format>/`. Hence the `forge.profile.properties` workaround.

## Reproducing the install

From the repo root, with `vendor/jre/` and `vendor/forge-installer-2.0.12.jar` already in place:

```powershell
./vendor/jre/bin/java.exe `
  -Djava.util.logging.config.file=setup/forge/logging.properties `
  -jar vendor/forge-installer-2.0.12.jar `
  setup/forge/auto-install.xml
```

After the installer finishes, copy `forge.profile.properties` into the install dir and seed userdata:

```powershell
Copy-Item setup/forge/forge.profile.properties vendor/forge/forge.profile.properties
New-Item -ItemType Directory -Force vendor/forge/userdata/decks/constructed | Out-Null
New-Item -ItemType Directory -Force vendor/forge/userdata/decks/commander | Out-Null
Copy-Item "vendor/forge/res/quest/precons/*.dck" vendor/forge/userdata/decks/constructed/
Copy-Item "vendor/forge/res/quest/commanderprecons/*.dck" vendor/forge/userdata/decks/commander/
```

Then run the verifier:

```powershell
python src/commander_builder/verify_forge.py
```

## Note on the install path

`auto-install.xml` hard-codes `C:\dev\commander_builder\vendor\forge` as the
install path. If your repo lives elsewhere, edit the `<installpath>` element
before running the installer.
