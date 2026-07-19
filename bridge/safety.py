# -*- coding: utf-8 -*-
"""Destructive-operation detection for execute_pyqgis code.

Runs on the QGIS side (in the socket worker thread) *before* any code reaches
the main-thread executor, so the approval gate applies no matter which subagent
issued the call — it is enforced at the bridge, never trusted to a prompt.

The scan is intentionally conservative: false positives cost one extra click,
false negatives risk data loss. AST first (robust), regex fallback for strings
the AST can't classify.
"""
import ast
import re

# Attribute/callable names that mutate files, layers, or the project on disk.
# Kept deliberately specific: broad names like dataProvider/clear/deleteLater
# are read-or-harmless in most PyQGIS and would nag on every turn.
_DESTRUCTIVE_CALLS = {
    "remove", "unlink", "rmtree", "rmdir", "removedirs",   # os / shutil
    "deleteFeatures", "deleteFeature",                     # provider deletes
    "changeAttributeValues", "dropField", "deleteAttribute", "deleteAttributes",
    "truncate",
    "commitChanges",
    "writeAsVectorFormat", "writeAsVectorFormatV3",
    "removeMapLayer", "removeMapLayers", "removeAllMapLayers",
}
# Module.function combinations worth flagging outright.
_DESTRUCTIVE_ATTR_PATHS = {
    ("os", "remove"), ("os", "unlink"), ("os", "rmdir"), ("os", "removedirs"),
    ("shutil", "rmtree"), ("shutil", "move"),
}

_REGEX_FALLBACK = re.compile(
    r"\b("
    r"os\.remove|os\.unlink|os\.rmdir|shutil\.rmtree|shutil\.move|"
    r"deleteFeatures?|removeMapLayers?|removeAllMapLayers|"
    r"QgsVectorFileWriter|commitChanges|\.write\s*\(|dropField|deleteAttributes?"
    r")\b"
)


def scan(code):
    """Return a list of human-readable reasons the code looks destructive.

    Empty list == safe to auto-run (subject to permission mode).
    """
    reasons = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        # Can't parse — fall back to regex and let the model see the traceback
        # anyway when it runs. Flag anything the regex catches.
        if _REGEX_FALLBACK.search(code):
            reasons.append("matched a destructive pattern (unparseable code)")
        return reasons

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            reason = _classify_call(node)
            if reason:
                reasons.append(reason)
        # Writing to open(...) in 'w'/'a'/'x' mode.
        if isinstance(node, ast.Call) and _is_open_write(node):
            reasons.append("opens a file for writing (open(..., 'w'/'a'))")

    # De-duplicate while preserving order.
    seen = set()
    unique = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    return unique


def _classify_call(node):
    func = node.func
    if isinstance(func, ast.Attribute):
        attr = func.attr
        # module.func path, e.g. os.remove
        if isinstance(func.value, ast.Name):
            path = (func.value.id, attr)
            if path in _DESTRUCTIVE_ATTR_PATHS:
                return f"calls {func.value.id}.{attr}() (filesystem mutation)"
        if attr in _DESTRUCTIVE_CALLS:
            return f"calls .{attr}() (potentially destructive)"
    elif isinstance(func, ast.Name):
        if func.id in {"QgsVectorFileWriter"}:
            return "constructs QgsVectorFileWriter (writes/overwrites a file)"
    return None


def _is_open_write(node):
    func = node.func
    if isinstance(func, ast.Name) and func.id == "open":
        # second positional arg or mode kwarg
        mode = None
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            mode = node.args[1].value
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                mode = kw.value.value
        if isinstance(mode, str) and any(m in mode for m in ("w", "a", "x", "+")):
            return True
    return False
