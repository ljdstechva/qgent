# -*- coding: utf-8 -*-
"""Self-check for image attachments and the attached-files prompt section.

Run directly: ``python qgis_chat_agent/tests/test_paste_attachment.py``
"""
from pathlib import Path
import sys
import tempfile
import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The section builder is pure string work, but its module imports qgis at the
# top. Stub it so this check runs outside QGIS too.
if "qgis" not in sys.modules:
    qgis = types.ModuleType("qgis")
    qgis.core = types.ModuleType("qgis.core")
    qgis.core.QgsProject = object

    class _WkbTypes:  # only the geometry enum members are read at import time
        PointGeometry = 0
        LineGeometry = 1
        PolygonGeometry = 2
        UnknownGeometry = 3
        NullGeometry = 4

    qgis.core.QgsWkbTypes = _WkbTypes
    sys.modules["qgis"] = qgis
    sys.modules["qgis.core"] = qgis.core

from context.project_snapshot import build_attached_files_section  # noqa: E402


def test_section_lists_images():
    with tempfile.TemporaryDirectory() as root:
        shot = Path(root) / "screenshot-20260806-120000-000000.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)
        section = build_attached_files_section([{
            "path": str(shot), "size": shot.stat().st_size,
            "extension": ".png", "file_kind": "PNG image", "warning": "",
        }])
    assert "PNG image" in section, section
    assert shot.name in section, section
    # The model has to be told to actually open the screenshot.
    assert "Read any image" in section, section


def test_section_is_empty_without_usable_entries():
    # No attachments at all.
    assert build_attached_files_section([]) == ""
    assert build_attached_files_section(None) == ""
    # Entries that cannot be described must not leave a header claiming files
    # are attached when none are listed.
    assert build_attached_files_section([{"bogus": True}]) == ""
    assert build_attached_files_section([{"path": "", "size": 0}]) == ""


def test_paste_target_is_an_accepted_attachment_kind():
    # The paste handler writes PNG; that extension must be attachable or the
    # screenshot would be saved and then silently rejected.
    source = (Path(__file__).resolve().parents[1] / "ui" / "chat_dock.py"
              ).read_text(encoding="utf-8")
    for marker in ('".png": "PNG image"', '_IMAGE_EXTENSIONS',
                   'def _attach_pasted_image',
                   # Pasting writes a file per screenshot; without pruning the
                   # profile grows without bound.
                   'def _prune_pasted_images', '_PASTED_KEEP'):
        assert marker in source, marker
    widgets = (Path(__file__).resolve().parents[1] / "ui" / "widgets.py"
               ).read_text(encoding="utf-8")
    # Qt routes Ctrl+V through insertFromMimeData; without the override the
    # screenshot would paste as nothing at all.
    assert "def insertFromMimeData" in widgets
    assert "def canInsertFromMimeData" in widgets
    assert "MIN_HEIGHT = 84" in widgets


def test_any_readable_file_is_attachable():
    """Attaching must not be gated on a whitelist of GIS extensions.

    QGent only ever hands the agent a path, so refusing a .pdf or a .txt
    blocked files the agent reads perfectly well.
    """
    source = (Path(__file__).resolve().parents[1] / "ui" / "chat_dock.py"
              ).read_text(encoding="utf-8")
    assert "def _attachment_kind" in source
    # The old gate rejected anything unlisted; it must be gone.
    assert "Unsupported file:" not in source, "whitelist rejection is back"
    assert 'return None, f"Folders cannot be attached' in source
    # Labels come from the helper, never a direct lookup that would KeyError.
    assert '"file_kind": _attachment_kind(extension)' in source
    for extension in (".pdf", ".xlsx", ".txt", ".md", ".sql", ".gpx"):
        assert f'"{extension}"' in source, extension


def demo():
    test_section_lists_images()
    test_section_is_empty_without_usable_entries()
    test_paste_target_is_an_accepted_attachment_kind()
    test_any_readable_file_is_attachable()
    print("paste attachment self-check: OK")


if __name__ == "__main__":
    demo()
