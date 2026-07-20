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
# checksum verification (security hardening)
# --------------------------------------------------------------------------- #
_PK_BYTES = b"PK\x03\x04"
_PK_SHA256 = "8dcc7e601606217f3b754766511182a916b17e9a26a94c9d887104eba92e9bb2"


def test_download_forge_verifies_matching_digest(tmp_path):
    release = {"assets": [{
        "name": "forge-gui-desktop-2.0.12-jar-with-dependencies.jar",
        "browser_download_url": "https://example/forge.jar",
        "digest": f"sha256:{_PK_SHA256}",
    }]}
    jar = bootstrap.download_forge(
        forge_dir=tmp_path / "forge",
        _get_release=lambda: release,
        _download=lambda url, dest: dest.write_bytes(_PK_BYTES),
    )
    assert jar.exists()


def test_download_forge_rejects_digest_mismatch(tmp_path):
    release = {"assets": [{
        "name": "forge-gui-desktop-2.0.12-jar-with-dependencies.jar",
        "browser_download_url": "https://example/forge.jar",
        "digest": "sha256:" + "0" * 64,
    }]}
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        bootstrap.download_forge(
            forge_dir=tmp_path / "forge",
            _get_release=lambda: release,
            _download=lambda url, dest: dest.write_bytes(_PK_BYTES),
        )
    # The bad download must not be left where check_dependencies finds it.
    assert not list((tmp_path / "forge").glob("*.jar"))


def test_download_jre_verifies_sha256_sidecar_asset(tmp_path):
    archive_name = "OpenJDK17U-jre_x64_windows_hotspot_17.0.11_9.zip"
    release = {"assets": [
        {"name": archive_name,
         "browser_download_url": "https://example/jre.zip"},
        {"name": f"{archive_name}.sha256.txt",
         "browser_download_url": "https://example/jre.zip.sha256.txt"},
    ]}

    def fake_download(url, dest):
        if url.endswith(".sha256.txt"):
            dest.write_text(f"{_PK_SHA256}  {archive_name}\n")
        else:
            dest.write_bytes(_PK_BYTES)

    archive = bootstrap.download_jre(
        jre_dir=tmp_path / "jre",
        system="Windows",
        machine="AMD64",
        _get_release=lambda: release,
        _download=fake_download,
    )
    assert archive.exists()


def test_download_jre_rejects_sidecar_mismatch(tmp_path):
    archive_name = "OpenJDK17U-jre_x64_windows_hotspot_17.0.11_9.zip"
    release = {"assets": [
        {"name": archive_name,
         "browser_download_url": "https://example/jre.zip"},
        {"name": f"{archive_name}.sha256",
         "browser_download_url": "https://example/jre.zip.sha256"},
    ]}

    def fake_download(url, dest):
        if url.endswith(".sha256"):
            dest.write_text(("0" * 64) + f"  {archive_name}\n")
        else:
            dest.write_bytes(_PK_BYTES)

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        bootstrap.download_jre(
            jre_dir=tmp_path / "jre",
            system="Windows",
            machine="AMD64",
            _get_release=lambda: release,
            _download=fake_download,
        )


def test_download_warns_but_succeeds_without_published_checksum(tmp_path, caplog):
    import logging
    release = {"assets": [{
        "name": "forge-gui-desktop-2.0.12-jar-with-dependencies.jar",
        "browser_download_url": "https://example/forge.jar",
    }]}
    with caplog.at_level(logging.WARNING, logger="commander_builder.bootstrap"):
        jar = bootstrap.download_forge(
            forge_dir=tmp_path / "forge",
            _get_release=lambda: release,
            _download=lambda url, dest: dest.write_bytes(_PK_BYTES),
        )
    assert jar.exists()
    assert "skipping integrity verification" in caplog.text


# --------------------------------------------------------------------------- #
# zip-slip guard (security hardening)
# --------------------------------------------------------------------------- #
def test_extract_jre_rejects_zip_slip_member(tmp_path):
    import zipfile

    archive = tmp_path / "jre.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../evil.txt", "outside")
    with pytest.raises(RuntimeError, match="zip-slip"):
        bootstrap.extract_jre(archive, jre_dir=tmp_path / "jre")
    assert not (tmp_path.parent / "evil.txt").exists()


def test_ensure_tar_members_within_rejects_traversal(tmp_path):
    """The manual tar guard used on Python < 3.12 (where extractall has
    no filter=) must reject a path-traversal member. Tested directly
    because on 3.12+ the filter='data' path handles it instead."""
    import tarfile

    archive = tmp_path / "jre.tar.gz"
    payload = tmp_path / "payload.txt"
    payload.write_text("x")
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(payload, arcname="../evil.txt")
    dest = tmp_path / "dest"
    dest.mkdir()
    with tarfile.open(archive, "r:gz") as tf:
        with pytest.raises(RuntimeError, match="zip-slip"):
            bootstrap._ensure_tar_members_within(tf, dest)


def test_ensure_tar_members_within_rejects_escaping_symlink(tmp_path):
    """A member whose own name is safe but whose symlink target escapes
    dest must also be rejected (filter='data' blocks these on 3.12+)."""
    import tarfile

    archive = tmp_path / "jre.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../etc/passwd"
        tf.addfile(info)
    dest = tmp_path / "dest"
    dest.mkdir()
    with tarfile.open(archive, "r:gz") as tf:
        with pytest.raises(RuntimeError, match="link target"):
            bootstrap._ensure_tar_members_within(tf, dest)


def test_ensure_tar_members_within_allows_safe_members(tmp_path):
    """A normal nested member must pass the guard unharmed."""
    import tarfile

    archive = tmp_path / "jre.tar.gz"
    payload = tmp_path / "payload.txt"
    payload.write_text("x")
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(payload, arcname="jdk-17/bin/java")
    dest = tmp_path / "dest"
    dest.mkdir()
    with tarfile.open(archive, "r:gz") as tf:
        bootstrap._ensure_tar_members_within(tf, dest)  # no raise


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


# --------------------------------------------------------------------------- #
# download_jre (FP-010 slice 1 -- injected HTTP, never touches the network)
# --------------------------------------------------------------------------- #
_JRE_RELEASE = {"assets": [
    {"name": "OpenJDK17U-jre_x64_windows_hotspot_17.0.11_9.zip",
     "browser_download_url": "https://example/jre-win.zip"},
    {"name": "OpenJDK17U-jre_x64_linux_hotspot_17.0.11_9.tar.gz",
     "browser_download_url": "https://example/jre-lin.tar.gz"},
]}


def test_download_jre_writes_archive(tmp_path):
    jre_dir = tmp_path / "jre"
    got = {}

    def fake_download(url, dest):
        got["url"] = url
        dest.write_bytes(b"PK\x03\x04")  # fake zip header

    arch = bootstrap.download_jre(
        jre_dir=jre_dir,
        system="Windows", machine="AMD64",
        _get_release=lambda: _JRE_RELEASE,
        _download=fake_download,
    )
    assert arch.exists()
    assert arch.parent == jre_dir
    assert got["url"] == "https://example/jre-win.zip"
    assert arch.name.endswith(".zip")


def test_download_jre_raises_when_no_matching_asset(tmp_path):
    with pytest.raises(RuntimeError, match="no Temurin JRE asset"):
        bootstrap.download_jre(
            jre_dir=tmp_path / "jre",
            system="FreeBSD", machine="i386",
            _get_release=lambda: _JRE_RELEASE,
            _download=lambda url, dest: None,
        )


def test_download_jre_creates_jre_dir(tmp_path):
    """download_jre creates the target directory when it does not exist."""
    jre_dir = tmp_path / "deep" / "jre"
    assert not jre_dir.exists()

    bootstrap.download_jre(
        jre_dir=jre_dir,
        system="Linux", machine="x86_64",
        _get_release=lambda: _JRE_RELEASE,
        _download=lambda url, dest: dest.write_bytes(b""),
    )
    assert jre_dir.exists()


# --------------------------------------------------------------------------- #
# prime_card_cache (FP-010 slice 1 -- injected lookup, no network)
# --------------------------------------------------------------------------- #
def test_prime_card_cache_primes_all_on_success():
    names = ["Sol Ring", "Command Tower", "Path to Exile"]
    result = bootstrap.prime_card_cache(
        names=names,
        _lookup=lambda n: {"name": n},  # always returns a hit
    )
    assert result["primed"] == names
    assert result["errors"] == []


def test_prime_card_cache_records_errors_on_miss():
    def _lookup(name):
        if name == "Nonexistent Card":
            return None
        return {"name": name}

    result = bootstrap.prime_card_cache(
        names=["Sol Ring", "Nonexistent Card", "Command Tower"],
        _lookup=_lookup,
    )
    assert "Sol Ring" in result["primed"]
    assert "Command Tower" in result["primed"]
    assert "Nonexistent Card" in result["errors"]


def test_prime_card_cache_records_errors_on_exception():
    def _lookup(name):
        raise RuntimeError("network timeout")

    result = bootstrap.prime_card_cache(
        names=["Sol Ring"],
        _lookup=_lookup,
    )
    assert result["primed"] == []
    assert "Sol Ring" in result["errors"]


def test_prime_card_cache_uses_default_list_when_names_none():
    """Calling prime_card_cache(names=None) uses the built-in default list."""
    called = []
    result = bootstrap.prime_card_cache(
        names=None,
        _lookup=lambda n: called.append(n) or {"name": n},
    )
    assert len(called) >= 5  # default list has at least 5 staples
    assert result["errors"] == []


# --------------------------------------------------------------------------- #
# TEMURIN_RELEASES_API constant present
# --------------------------------------------------------------------------- #
def test_temurin_releases_api_constant():
    assert "adoptium" in bootstrap.TEMURIN_RELEASES_API
    assert "temurin17" in bootstrap.TEMURIN_RELEASES_API
