"""Tests for the FP-010 first-run dependency detection + Forge downloader."""
from __future__ import annotations

import pytest

from commander_builder import bootstrap


def _fat_jar(forge_dir, version):
    p = forge_dir / f"forge-gui-desktop-{version}-jar-with-dependencies.jar"
    p.write_bytes(b"PK\x03\x04")
    return p


# --------------------------------------------------------------------------- #
# check_dependencies
# --------------------------------------------------------------------------- #
def test_check_dependencies_all_present(tmp_path):
    forge = tmp_path / "forge"; forge.mkdir()
    _fat_jar(forge, "2.0.12")
    jre = tmp_path / "jre" / "bin"; jre.mkdir(parents=True)
    (jre / "java.exe").write_bytes(b"")
    cards = tmp_path / "mtg_cards"; cards.mkdir()

    st = bootstrap.check_dependencies(forge_dir=forge, jre_dir=tmp_path / "jre",
                                      cards_dir=cards)
    assert st.forge_present and st.jre_present and st.cards_present
    assert st.all_present and st.missing == []
    assert st.forge_jar.name.endswith("2.0.12-jar-with-dependencies.jar")


def test_check_dependencies_reports_missing_forge(tmp_path):
    forge = tmp_path / "forge"; forge.mkdir()  # empty -> no jar
    jre = tmp_path / "jre" / "bin"; jre.mkdir(parents=True)
    (jre / "java.exe").write_bytes(b"")  # jre present via file

    st = bootstrap.check_dependencies(forge_dir=forge, jre_dir=tmp_path / "jre",
                                      cards_dir=tmp_path / "nope")  # cards absent
    assert not st.forge_present and st.jre_present and not st.cards_present
    assert "forge" in st.missing and "mtg_cards" in st.missing
    assert any("Forge jar missing" in n for n in st.notes)


def test_check_dependencies_picks_highest_forge_version(tmp_path):
    forge = tmp_path / "forge"; forge.mkdir()
    _fat_jar(forge, "2.0.9")
    _fat_jar(forge, "2.0.12")
    st = bootstrap.check_dependencies(forge_dir=forge, jre_dir=tmp_path / "jre",
                                      cards_dir=tmp_path)
    assert "2.0.12" in st.forge_jar.name  # version-aware, not lexicographic


# --------------------------------------------------------------------------- #
# _pick_forge_jar_asset
# --------------------------------------------------------------------------- #
def test_pick_forge_jar_asset_selects_desktop_fat_jar():
    release = {"assets": [
        {"name": "forge-gui-android-2.0.12.apk", "browser_download_url": "u1"},
        {"name": "forge-gui-desktop-2.0.12.tar.bz2", "browser_download_url": "u2"},
        {"name": "forge-gui-desktop-2.0.12-jar-with-dependencies.jar",
         "browser_download_url": "u3"},
    ]}
    asset = bootstrap._pick_forge_jar_asset(release)
    assert asset["browser_download_url"] == "u3"


def test_pick_forge_jar_asset_none_when_absent():
    assert bootstrap._pick_forge_jar_asset({"assets": [
        {"name": "forge-gui-android-2.0.12.apk", "browser_download_url": "u"},
    ]}) is None
    assert bootstrap._pick_forge_jar_asset({}) is None


# --------------------------------------------------------------------------- #
# download_forge (injected HTTP)
# --------------------------------------------------------------------------- #
def test_download_forge_writes_asset(tmp_path):
    forge = tmp_path / "forge"
    release = {"assets": [{
        "name": "forge-gui-desktop-2.0.12-jar-with-dependencies.jar",
        "browser_download_url": "https://example/forge.jar",
    }]}
    got = {}

    def fake_download(url, dest):
        got["url"] = url
        dest.write_bytes(b"PK\x03\x04")

    jar = bootstrap.download_forge(
        forge_dir=forge,
        _get_release=lambda: release,
        _download=fake_download,
    )
    assert jar.exists()
    assert jar.name == "forge-gui-desktop-2.0.12-jar-with-dependencies.jar"
    assert got["url"] == "https://example/forge.jar"


def test_download_forge_raises_when_no_asset(tmp_path):
    with pytest.raises(RuntimeError, match="no forge-gui-desktop"):
        bootstrap.download_forge(
            forge_dir=tmp_path / "forge",
            _get_release=lambda: {"assets": []},
            _download=lambda url, dest: None,
        )


# --------------------------------------------------------------------------- #
# _pick_jre_asset -- platform JRE selection from a Temurin release (FP-010)
# --------------------------------------------------------------------------- #
def test_pick_jre_asset_selects_platform_archive():
    release = {"assets": [
        {"name": "OpenJDK17U-jre_x64_windows_hotspot_17.0.11_9.zip",
         "browser_download_url": "win"},
        {"name": "OpenJDK17U-jre_x64_linux_hotspot_17.0.11_9.tar.gz",
         "browser_download_url": "lin"},
        {"name": "OpenJDK17U-jre_aarch64_mac_hotspot_17.0.11_9.tar.gz",
         "browser_download_url": "mac"},
    ]}
    assert bootstrap._pick_jre_asset(release, "Windows", "AMD64")["browser_download_url"] == "win"
    assert bootstrap._pick_jre_asset(release, "Linux", "x86_64")["browser_download_url"] == "lin"
    assert bootstrap._pick_jre_asset(release, "Darwin", "arm64")["browser_download_url"] == "mac"
    assert bootstrap._pick_jre_asset({"assets": []}, "Windows", "AMD64") is None
