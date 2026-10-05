"""Installer boundary and rollback checks, without network or QGIS."""
import io
import json
from pathlib import Path
import stat
import sys
from zipfile import ZipFile, ZipInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import update_install as installer

SHA = "a" * 40
META = ("[general]\nname=QGent\nrepository=" + installer.REPOSITORY
        + "\nversion=0.5.0\nqgisMinimumVersion=3.28\nqgisMaximumVersion=4.99\n")


def archive(extra=(), metadata=META):
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as handle:
        for name, data in [("metadata.txt", metadata), ("__init__.py", ""),
                           ("plugin.py", "NEW = True\n"), *extra]:
            if isinstance(name, str):
                raw = "qgent-" + SHA + "/" + name
                name = ZipInfo()
                name.filename = raw  # Keep malicious paths verbatim even on Windows.
            handle.writestr(name, data)
    return buffer.getvalue()


@pytest.fixture
def install(tmp_path):
    plugin = tmp_path / "python" / "plugins" / "qgent"
    plugin.mkdir(parents=True)
    (plugin / "metadata.txt").write_text(META.replace("0.5.0", "0.4.1"))
    (plugin / "plugin.py").write_text("OLD = True\n")
    runtime = plugin / installer.RUNTIME_CONFIG
    runtime.parent.mkdir()
    runtime.write_text("runtime fixture")
    (plugin / ".git").mkdir()
    (plugin / ".git" / "HEAD").write_text("checkout fixture")
    (plugin / "local-work.txt").write_text("keep in backup")
    def prepare(data=None, **kwargs):
        def fetch(url, _limit, _cancelled):
            if url == installer.COMMIT_URL:
                return json.dumps({"sha": SHA}).encode()
            assert url == installer.ARCHIVE_URL + SHA
            return archive() if data is None else data
        return installer.prepare_update(tmp_path, plugin, "0.5.0", 34408,
                                        fetcher=fetch, **kwargs)
    return tmp_path, plugin, prepare


def test_verified_install_preserves_runtime_backup_and_rollback(install):
    _, plugin, prepare = install
    prepared = prepare()
    assert (plugin / "plugin.py").read_text() == "OLD = True\n"
    installer.install_update(prepared, plugin)
    assert (plugin / "plugin.py").read_text() == "NEW = True\n"
    assert (plugin / installer.RUNTIME_CONFIG).read_text() == "runtime fixture"
    backup = prepared["backup"]
    assert (backup / "local-work.txt").read_text() == "keep in backup"
    assert (backup / ".git" / "HEAD").read_text() == "checkout fixture"
    installer.discard_update(prepared)
    assert backup.exists()
    installer.rollback_update(prepared, plugin)
    assert (plugin / "plugin.py").read_text() == "OLD = True\n"
    assert (plugin / "local-work.txt").exists()


@pytest.mark.parametrize("name", ["../outside", "/absolute", "C:/absolute", "a\\b",
                                     "a/../../outside", "a:stream", "CON", "a.",
                                     ".git/config", "__pycache__/x.pyc",
                                     "claude_runtime/mcp-config.json", "PLUGIN.py"])
def test_rejects_unsafe_archive_without_touching_install(install, name):
    root, plugin, prepare = install
    with pytest.raises(ValueError):
        prepare(archive([(name, "bad")]))
    assert (plugin / "plugin.py").read_text() == "OLD = True\n"
    assert not list((root / "qgent" / "updates").glob("update-*"))


def test_symlink_size_invalid_metadata_and_python_are_rejected(install, monkeypatch):
    root, plugin, prepare = install
    link = ZipInfo("qgent-" + SHA + "/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    for data in [archive([(link, "../outside")]),
                 archive(metadata=META.replace("0.5.0", "0.6.0")),
                 archive(metadata=META.replace("name=QGent", "name=Other")),
                 archive(metadata=META.replace("ljdstechva", "other")),
                 archive(metadata=META.replace("3.28", "4.0")),
                 archive([("broken.py", "this is not python !!!")]), b"not a zip"]:
        with pytest.raises(Exception):
            prepare(data)
        assert (plugin / "plugin.py").read_text() == "OLD = True\n"
    monkeypatch.setattr(installer, "MAX_EXPANDED", 1)
    with pytest.raises(ValueError, match="expanded size"):
        prepare()
    assert not list((root / "qgent" / "updates").glob("update-*"))


def test_cancel_offline_stale_version_and_source_tree_are_safe(install):
    root, plugin, prepare = install
    with pytest.raises(InterruptedError):
        prepare(cancelled=lambda: True)
    prepared = prepare()
    (plugin / "metadata.txt").write_text(META)
    with pytest.raises(ValueError, match="changed during download"):
        installer.install_update(prepared, plugin)
    installer.discard_update(prepared)
    with pytest.raises(ValueError, match="No newer"):
        prepare()
    with pytest.raises(ValueError, match="inside your QGIS profile"):
        installer.installed_target(root, root)
    (plugin / "metadata.txt").write_text(META.replace("0.5.0", "0.4.1"))
    def offline(*_args):
        raise TimeoutError("offline")
    with pytest.raises(TimeoutError):
        installer.prepare_update(root, plugin, "0.5.0", 34408, fetcher=offline)
    assert (plugin / "plugin.py").read_text() == "OLD = True\n"


def test_failed_second_rename_restores_original(install, monkeypatch):
    _, plugin, prepare = install
    prepared = prepare()
    rename = Path.rename
    def locked(source, target):
        if source == prepared["staged"]:
            raise PermissionError("simulated file lock")
        return rename(source, target)
    monkeypatch.setattr(Path, "rename", locked)
    with pytest.raises(PermissionError):
        installer.install_update(prepared, plugin)
    assert (plugin / "plugin.py").read_text() == "OLD = True\n"
    assert not prepared["backup"].exists()


def test_untrusted_commit_is_rejected(install):
    root, plugin, _ = install
    for sha in ("main", "../other", "a" * 39, 42):
        with pytest.raises(ValueError, match="invalid commit"):
            installer.prepare_update(root, plugin, "0.5.0", 34408,
                                     fetcher=lambda *_: json.dumps({"sha": sha}).encode())


def test_download_limit_redirect_and_cancellation(monkeypatch):
    class Response(io.BytesIO):
        def geturl(self):
            return installer.ARCHIVE_URL + SHA
    monkeypatch.setattr(installer, "urlopen", lambda *_args, **_kwargs: Response(b"payload"))
    assert installer.download(installer.ARCHIVE_URL + SHA, 20, lambda: False) == b"payload"
    with pytest.raises(ValueError, match="size limit"):
        installer.download(installer.ARCHIVE_URL + SHA, 2, lambda: False)
    with pytest.raises(InterruptedError):
        installer.download(installer.ARCHIVE_URL + SHA, 20, lambda: True)
    monkeypatch.setattr(Response, "geturl", lambda _: "https://other.invalid/archive")
    with pytest.raises(ValueError, match="redirect"):
        installer.download(installer.ARCHIVE_URL + SHA, 20, lambda: False)
