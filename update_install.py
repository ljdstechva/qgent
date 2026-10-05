"""Stage and replace QGent from its fixed upstream repository (stdlib only)."""
from configparser import ConfigParser
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from zipfile import ZipFile

try:
    from .update_check import version_tuple
except ImportError:  # standalone checks
    from update_check import version_tuple


REPOSITORY = "https://github.com/ljdstechva/qgent"
COMMIT_URL = "https://api.github.com/repos/ljdstechva/qgent/commits/main"
ARCHIVE_URL = "https://codeload.github.com/ljdstechva/qgent/zip/"
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
RUNTIME_CONFIG = Path("claude_runtime/mcp-config.json")


def download(url, limit, cancelled):
    request = Request(url, headers={"User-Agent": "QGent-update-install/1"})
    deadline = time.monotonic() + 90
    with urlopen(request, timeout=10) as response:
        actual = urlsplit(response.geturl())
        if actual.scheme != "https" or actual.netloc != urlsplit(url).netloc:
            raise ValueError("Unexpected update download redirect")
        chunks, size = [], 0
        while True:
            if cancelled():
                raise InterruptedError("Update cancelled")
            if time.monotonic() > deadline:
                raise TimeoutError("Update download timed out")
            chunk = response.read(min(65536, limit + 1 - size))
            size += len(chunk)
            if size > limit:
                raise ValueError("Update download exceeded the size limit")
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)


def installed_target(profile_dir, plugin_dir):
    """Only replace a real plugin inside the current profile, never a dev tree."""
    plugin = Path(plugin_dir).absolute()
    plugins = Path(profile_dir).resolve() / "python" / "plugins"
    if (plugin.resolve() != plugin or plugin.parent != plugins.resolve()
            or not (plugin / "metadata.txt").is_file()):
        raise ValueError("Install updates from QGent inside your QGIS profile")
    return plugin


def _extract(data, target, commit, cancelled):
    seen = set()
    with ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 5000 or sum(entry.file_size for entry in entries) > MAX_EXPANDED:
            raise ValueError("Update archive exceeded the expanded size limit")
        for entry in entries:
            if cancelled():
                raise InterruptedError("Update cancelled")
            # ZipInfo normalizes Windows backslashes and truncates NULs in filename.
            name = entry.orig_filename
            parts = name.rstrip("/").split("/")
            mode = entry.external_attr >> 16
            if (parts[0] != "qgent-" + commit or "\\" in name
                    or any(part in ("", ".", "..") or re.search(r'[<>:"|?*\x00-\x1f]', part)
                           or part.endswith((".", " "))
                           or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)
                           for part in parts)
                    or stat.S_ISLNK(mode)
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise ValueError("Unsafe path or file type in update archive")
            key = name.rstrip("/").casefold()
            if key in seen:
                raise ValueError("Duplicate path in update archive")
            seen.add(key)
            relative = PurePosixPath(*parts[1:])
            if ".git" in relative.parts or "__pycache__" in relative.parts or relative.suffix == ".pyc":
                raise ValueError("Unexpected repository/cache files in update archive")
            if relative.as_posix() == RUNTIME_CONFIG.as_posix():
                raise ValueError("Update archive must not contain runtime credentials")
            destination = target.joinpath(*relative.parts)
            if entry.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                if not relative.parts:
                    raise ValueError("Invalid update archive root")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, destination.open("xb") as output:
                    shutil.copyfileobj(source, output)


def prepare_update(profile_dir, plugin_dir, expected_version, qgis_version,
                   *, fetcher=download, cancelled=lambda: False):
    plugin = installed_target(profile_dir, plugin_dir)
    current = ConfigParser(interpolation=None)
    current.read(plugin / "metadata.txt", encoding="utf-8")
    if version_tuple(expected_version) <= version_tuple(current.get("general", "version")):
        raise ValueError("No newer QGent version to install; check for updates again")
    commit = json.loads(fetcher(COMMIT_URL, 1024 * 1024, cancelled)).get("sha", "")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("GitHub returned an invalid commit")
    archive = fetcher(ARCHIVE_URL + commit, MAX_ARCHIVE, cancelled)
    updates = Path(profile_dir).resolve() / "qgent" / "updates"
    updates.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="update-", dir=updates))
    staged = workspace / "new"
    try:
        _extract(archive, staged, commit, cancelled)
        metadata = ConfigParser(interpolation=None)
        metadata.read(staged / "metadata.txt", encoding="utf-8")
        if (metadata.get("general", "name") != "QGent"
                or metadata.get("general", "repository").rstrip("/") != REPOSITORY
                or metadata.get("general", "version") != expected_version):
            raise ValueError("Update metadata changed or is invalid; check for updates again")
        for name in ("__init__.py", "plugin.py"):
            if not (staged / name).is_file():
                raise ValueError("Incomplete QGent update package")
        for key, default, minimum in (("qgisMinimumVersion", "0.0", True),
                                     ("qgisMaximumVersion", "99.99", False)):
            value = metadata.get("general", key, fallback=default)
            if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", value):
                raise ValueError("Invalid QGIS compatibility version")
            numbers = [int(part) for part in value.split(".")]
            if len(numbers) == 2:
                numbers.append(0 if minimum else 99)
            bound = numbers[0] * 10000 + numbers[1] * 100 + numbers[2]
            if (minimum and qgis_version < bound) or (not minimum and qgis_version > bound):
                raise ValueError("This update is not compatible with your QGIS version")
        for path in staged.rglob("*.py"):
            if cancelled():
                raise InterruptedError("Update cancelled")
            compile(path.read_bytes(), str(path), "exec")
        return {"staged": staged, "backup": workspace / "previous", "commit": commit,
                "version": expected_version, "installed_version": current.get("general", "version")}
    except BaseException:
        shutil.rmtree(workspace)
        raise


def install_update(prepared, plugin_dir):
    """Call after QGent unloads; rename on one filesystem and keep the old tree."""
    plugin = Path(plugin_dir)
    staged, backup = prepared["staged"], prepared["backup"]
    current = ConfigParser(interpolation=None)
    current.read(plugin / "metadata.txt", encoding="utf-8")
    if current.get("general", "version") != prepared["installed_version"]:
        raise ValueError("The installed plugin changed during download; check again")
    runtime = plugin / RUNTIME_CONFIG
    if runtime.is_file():
        if runtime.resolve() != runtime.absolute():
            raise ValueError("Cannot replace a plugin with linked runtime configuration")
        (staged / RUNTIME_CONFIG).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(runtime, staged / RUNTIME_CONFIG)
    plugin.rename(backup)
    try:
        staged.rename(plugin)
    except OSError:
        # A failed second rename must leave the original at its original path.
        backup.rename(plugin)
        raise


def rollback_update(prepared, plugin_dir):
    plugin = Path(plugin_dir)
    failed = prepared["backup"].parent / "failed"
    plugin.rename(failed)
    try:
        prepared["backup"].rename(plugin)
    except OSError:
        failed.rename(plugin)
        raise


def discard_update(prepared):
    """Delete only unused staging; retained backups are never pruned automatically."""
    if prepared and not prepared["backup"].exists():
        shutil.rmtree(prepared["staged"].parent)
