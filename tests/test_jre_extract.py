"""Tests for FP-010 JRE extraction (extract_jre / ensure_jre)."""
import tarfile
import zipfile
from pathlib import Path

import pytest

from commander_builder import bootstrap


def _make_zip(path: Path, root: str) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{root}/bin/java.exe", b"binary")
        zf.writestr(f"{root}/lib/modules", b"libdata")
        zf.writestr(f"{root}/release", b"JAVA_VERSION=17")


def _make_targz(path: Path, root: str) -> None:
    import io
    with tarfile.open(path, "w:gz") as tf:
        for rel, data in [
            (f"{root}/bin/java", b"binary"),
            (f"{root}/lib/modules", b"libdata"),
        ]:
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def test_extract_zip_flattens_nested_root(tmp_path):
    arch = tmp_path / "OpenJDK17U-jre_x64_windows.zip"
    _make_zip(arch, "jdk-17.0.11+9-jre")
    jre = tmp_path / "jre"
    out = bootstrap.extract_jre(arch, jre)
    assert out == jre
    assert (jre / "bin" / "java.exe").exists()      # nested root flattened away
    assert (jre / "lib" / "modules").exists()
    assert not (jre / "jdk-17.0.11+9-jre").exists()


def test_extract_targz_flattens_nested_root(tmp_path):
    arch = tmp_path / "OpenJDK17U-jre_x64_linux.tar.gz"
    _make_targz(arch, "jdk-17.0.11+9-jre")
    jre = tmp_path / "jre"
    bootstrap.extract_jre(arch, jre)
    assert (jre / "bin" / "java").exists()


def test_extract_unsupported_archive_raises(tmp_path):
    arch = tmp_path / "jre.7z"
    arch.write_bytes(b"not a real archive")
    with pytest.raises(RuntimeError, match="unsupported"):
        bootstrap.extract_jre(arch, tmp_path / "jre")


def test_extract_without_bin_java_raises(tmp_path):
    arch = tmp_path / "bogus.zip"
    with zipfile.ZipFile(arch, "w") as zf:
        zf.writestr("jdk-x/readme.txt", b"no java here")
    with pytest.raises(RuntimeError, match="bin/java"):
        bootstrap.extract_jre(arch, tmp_path / "jre")


def test_ensure_jre_skips_when_already_installed(tmp_path):
    jre = tmp_path / "jre"
    (jre / "bin").mkdir(parents=True)
    (jre / "bin" / "java.exe").write_bytes(b"already here")

    def _boom():  # _get_release must NOT be called
        raise AssertionError("ensure_jre downloaded despite existing java")

    out = bootstrap.ensure_jre(jre, _get_release=_boom)
    assert out == jre


def test_ensure_jre_downloads_and_extracts_when_missing(tmp_path):
    jre = tmp_path / "jre"
    staged = tmp_path / "staged.zip"
    _make_zip(staged, "jdk-17.0.11+9-jre")

    release = {"assets": [{
        "name": "OpenJDK17U-jre_x64_windows_hotspot_17.0.11_9.zip",
        "browser_download_url": "https://example/jre.zip",
    }]}

    def _get_release():
        return release

    def _download(url, dest):
        # download_jre writes to jre_dir/asset["name"]; mimic by copying staged.
        dest.write_bytes(staged.read_bytes())

    out = bootstrap.ensure_jre(
        jre, system="Windows", machine="AMD64",
        _get_release=_get_release, _download=_download,
    )
    assert (out / "bin" / "java.exe").exists()
