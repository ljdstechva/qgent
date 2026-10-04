"""The layout tools are reachable from both backends and gated by the bridge."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from bridge import mcp_stdio_bridge, safety

ROOT = Path(__file__).resolve().parents[1]


def _tool(name):
    return next(tool for tool in mcp_stdio_bridge.TOOLS if tool["name"] == name)


def test_catalogue_exposes_both_layout_tools():
    info = _tool("layout_info")
    manage = _tool("manage_layouts")
    assert info["inputSchema"]["properties"]["action"]["enum"] == [
        "list", "describe", "render"]
    assert manage["inputSchema"]["properties"]["action"]["enum"] == [
        "build", "save_template", "create_from_template", "export", "open",
        "delete"]
    assert "approval" in manage["description"]


def test_ask_user_description_forbids_prose_questions():
    assert "never end a turn with a plain-text question" in \
        _tool("ask_user")["description"]


@pytest.mark.parametrize("relative, names", [
    ("agent/claude_code_backend.py",
     ["mcp__qgis__layout_info", "mcp__qgis__manage_layouts"]),
    ("agent/codex_backend.py", ['"layout_info"', '"manage_layouts"']),
])
def test_backends_allow_the_layout_tools(relative, names):
    source = (ROOT / relative).read_text(encoding="utf-8")
    for name in names:
        assert name in source


def test_claude_builtin_question_tool_is_disallowed():
    source = (ROOT / "agent/claude_code_backend.py").read_text(encoding="utf-8")
    disallowed = re.search(r"_DISALLOWED_TOOLS = \",\"\.join\(\[(.*?)\]\)",
                           source, re.DOTALL).group(1)
    assert '"AskUserQuestion"' in disallowed


def test_read_only_verifier_gets_only_the_read_only_layout_tool():
    verifier = (ROOT / "claude_runtime/.claude/agents/qa-verifier.md") \
        .read_text(encoding="utf-8")
    tools_line = next(line for line in verifier.splitlines()
                      if line.startswith("tools:"))
    assert "mcp__qgis__layout_info" in tools_line
    assert "manage_layouts" not in tools_line


@pytest.mark.parametrize("args, expected", [
    ({"action": "delete", "layout": "Plan"}, "deletes print layout 'Plan'"),
    ({"action": "build", "spec": {"name": "Plan", "replace": True}},
     "replaces print layout 'Plan'"),
    ({"action": "build", "spec": '{"name": "Plan", "replace": true}'},
     "replaces print layout 'Plan'"),
    ({"action": "create_from_template", "layout_name": "B", "replace": True},
     "replaces print layout 'B'"),
    ({"action": "save_template", "layout": "Plan", "name": "T",
      "overwrite": True}, "overwrites 'T'"),
    ({"action": "export", "layout": "Plan", "path": "C:/x.pdf",
      "overwrite": True}, "overwrites 'C:/x.pdf'"),
])
def test_destructive_layout_actions_need_approval(args, expected):
    reasons = safety.layout_reasons(args)
    assert len(reasons) == 1 and reasons[0].startswith(expected)


@pytest.mark.parametrize("args", [
    {"action": "build", "spec": {"name": "Plan"}},
    {"action": "build", "spec": {"name": "Plan", "mode": "update",
                                 "replace": True}},
    {"action": "save_template", "layout": "Plan"},
    {"action": "create_from_template", "template": "T", "layout_name": "B"},
    {"action": "export", "layout": "Plan", "path": "C:/x.pdf"},
    {"action": "open", "layout": "Plan"},
    "not a dict",
])
def test_non_destructive_layout_actions_pass(args):
    assert safety.layout_reasons(args) == []


@pytest.mark.parametrize("code", [
    "QgsProject.instance().layoutManager().removeLayout(old)",
    "layout.saveAsTemplate('C:/t.qpt', QgsReadWriteContext())",
])
def test_raw_layout_removal_and_template_writes_are_gated(code):
    assert safety.scan(code)


def test_skill_and_rules_document_the_tools():
    skill = (ROOT / "claude_runtime/.claude/skills/layout-templates/SKILL.md")
    text = skill.read_text(encoding="utf-8")
    for needle in ("manage_layouts", "layout_info", "save_template",
                   "create_from_template", "render", "[Map Title]"):
        assert needle in text
    for rules in ("claude_runtime/CLAUDE.md", "claude_runtime/AGENTS.md"):
        body = (ROOT / rules).read_text(encoding="utf-8")
        assert "layout-templates" in body
        assert "manage_layouts" in body
        assert "plain-text question" in body
