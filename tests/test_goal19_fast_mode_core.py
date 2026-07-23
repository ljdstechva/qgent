from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


class _Signal:
    def __init__(self) -> None:
        self.callbacks = []
        self.emissions = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)

    def disconnect(self) -> None:
        self.callbacks.clear()

    def emit(self, *args) -> None:
        self.emissions.append(args)


class _FakeProcessEnvironment:
    def __init__(self) -> None:
        self.values = {}

    @classmethod
    def systemEnvironment(cls):  # noqa: N802 - mirrors the Qt API
        return cls()

    def insert(self, key, value) -> None:
        self.values[key] = value


class _FakeProcess:
    NotRunning = 0
    FailedToStart = 1
    starts = []

    def __init__(self, _parent=None) -> None:
        self.readyReadStandardOutput = _Signal()
        self.finished = _Signal()
        self.errorOccurred = _Signal()
        self._state = self.NotRunning
        self.args = []

    def state(self):
        return self._state

    def setWorkingDirectory(self, _path):  # noqa: N802
        pass

    def setProcessEnvironment(self, _environment):  # noqa: N802
        pass

    def start(self, executable, args):
        self._state = 1
        self.args = list(args)
        self.starts.append((executable, list(args)))

    def waitForStarted(self, _timeout):  # noqa: N802
        return True

    def write(self, _data):
        pass

    def closeWriteChannel(self):  # noqa: N802
        pass

    def kill(self):
        self._state = self.NotRunning

    def deleteLater(self):  # noqa: N802
        pass

    def readAllStandardOutput(self):  # noqa: N802
        return b""

    def readAllStandardError(self):  # noqa: N802
        return b""


class _FakeAgentBackend:
    def __init__(self, runtime_dir, mcp_config_path, env, parent=None):
        del parent
        self.runtime_dir = runtime_dir
        self.mcp_config_path = mcp_config_path
        self.env = env
        self.session_id = None
        self.last_stderr = ""
        for name in (
            "token", "tool_call", "tool_result", "subagent_event",
            "session_started", "done", "error", "status_note", "busy_changed",
        ):
            setattr(self, name, _Signal())


class _FakeParser:
    def reset(self):
        pass

    def feed(self, _text):
        return []


class _FakeCrs:
    def __init__(self, authid: str) -> None:
        self._authid = authid

    def authid(self) -> str:
        return self._authid

    def isValid(self) -> bool:  # noqa: N802 - mirrors the QGIS API
        return bool(self._authid)


class _FakeWkbTypes:
    PointGeometry = 0
    LineGeometry = 1
    PolygonGeometry = 2
    NullGeometry = 3
    UnknownGeometry = 4

    @staticmethod
    def geometryType(wkb_type):  # noqa: N802 - mirrors the QGIS API
        return wkb_type

    @staticmethod
    def displayString(wkb_type):  # noqa: N802 - mirrors the QGIS API
        return {
            0: "Point",
            1: "LineString",
            2: "Polygon",
        }.get(wkb_type, "Unknown")


class _FakeProjectApi:
    current = None

    @classmethod
    def instance(cls):
        return cls.current


class _FakeLayer:
    def __init__(self, index: int) -> None:
        self.index = index
        self.count_calls = 0

    def id(self) -> str:
        return f"id_{self.index}"

    def name(self) -> str:
        return f"layer_{self.index}"

    def crs(self):
        return _FakeCrs("EPSG:32651")

    def wkbType(self):  # noqa: N802 - mirrors the QGIS API
        return _FakeWkbTypes.LineGeometry

    def featureCount(self):  # noqa: N802 - mirrors the QGIS API
        self.count_calls += 1
        return self.index + 1


class _FakeExtent:
    def xMinimum(self):  # noqa: N802 - mirrors the QGIS API
        return 1.2

    def yMinimum(self):  # noqa: N802 - mirrors the QGIS API
        return 2.6

    def xMaximum(self):  # noqa: N802 - mirrors the QGIS API
        return 4.1

    def yMaximum(self):  # noqa: N802 - mirrors the QGIS API
        return 5.8


class _FakeMapSettings:
    def destinationCrs(self):  # noqa: N802 - mirrors the QGIS API
        return _FakeCrs("EPSG:32651")


class _FakeCanvas:
    def extent(self):
        return _FakeExtent()

    def mapSettings(self):  # noqa: N802 - mirrors the QGIS API
        return _FakeMapSettings()


class _FakeProject:
    def __init__(self, layers) -> None:
        self.layers = layers

    def crs(self):
        return _FakeCrs("EPSG:32651")

    def title(self):
        return "Fast fixture"

    def fileName(self):  # noqa: N802 - mirrors the QGIS API
        return ""

    def mapLayers(self):  # noqa: N802 - mirrors the QGIS API
        return {layer.id(): layer for layer in self.layers}


class _FakeIface:
    def __init__(self, active_layer) -> None:
        self._active_layer = active_layer

    def mapCanvas(self):  # noqa: N802 - mirrors the QGIS API
        return _FakeCanvas()

    def activeLayer(self):  # noqa: N802 - mirrors the QGIS API
        return self._active_layer


@pytest.fixture
def snapshot_module(monkeypatch):
    qgis_module = types.ModuleType("qgis")
    core_module = types.ModuleType("qgis.core")
    core_module.QgsProject = _FakeProjectApi
    core_module.QgsWkbTypes = _FakeWkbTypes
    qgis_module.core = core_module
    monkeypatch.setitem(sys.modules, "qgis", qgis_module)
    monkeypatch.setitem(sys.modules, "qgis.core", core_module)

    path = Path(__file__).parents[1] / "context" / "project_snapshot.py"
    spec = importlib.util.spec_from_file_location(
        "qgent_goal19_project_snapshot", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def backend_loader(monkeypatch):
    qgis_module = types.ModuleType("qgis")
    pyqt_module = types.ModuleType("qgis.PyQt")
    qtcore_module = types.ModuleType("qgis.PyQt.QtCore")
    qtcore_module.QProcess = _FakeProcess
    qtcore_module.QProcessEnvironment = _FakeProcessEnvironment
    pyqt_module.QtCore = qtcore_module
    qgis_module.PyQt = pyqt_module
    monkeypatch.setitem(sys.modules, "qgis", qgis_module)
    monkeypatch.setitem(sys.modules, "qgis.PyQt", pyqt_module)
    monkeypatch.setitem(sys.modules, "qgis.PyQt.QtCore", qtcore_module)

    package = types.ModuleType("qgis_chat_agent")
    package.__path__ = []
    agent_package = types.ModuleType("qgis_chat_agent.agent")
    agent_package.__path__ = []
    backend_base = types.ModuleType("qgis_chat_agent.agent.backend_base")
    backend_base.AgentBackend = _FakeAgentBackend
    backend_base.normalized_usage = lambda _backend, _payload: None
    stream_parser = types.ModuleType("qgis_chat_agent.agent.stream_parser")
    stream_parser.StreamJsonParser = _FakeParser
    config_module = types.ModuleType("qgis_chat_agent.config")
    config_module.model_id = {
        "codex": "gpt-5.6-sol",
        "claude": "fable",
    }

    def validate_model_choice(backend, _role):
        return {
            "model_id": config_module.model_id[backend],
            "custom": False,
            "note": "",
        }

    config_module.validate_model_choice = validate_model_choice
    monkeypatch.setitem(sys.modules, "qgis_chat_agent", package)
    monkeypatch.setitem(sys.modules, "qgis_chat_agent.agent", agent_package)
    monkeypatch.setitem(
        sys.modules, "qgis_chat_agent.agent.backend_base", backend_base)
    monkeypatch.setitem(
        sys.modules, "qgis_chat_agent.agent.stream_parser", stream_parser)
    monkeypatch.setitem(sys.modules, "qgis_chat_agent.config", config_module)
    _FakeProcess.starts = []

    def load(name):
        path = Path(__file__).parents[1] / "agent" / f"{name}.py"
        qualified = f"qgis_chat_agent.agent.{name}"
        spec = importlib.util.spec_from_file_location(qualified, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, qualified, module)
        spec.loader.exec_module(module)
        return module, config_module

    return load


def test_fast_context_is_capped_count_free_and_integer(snapshot_module):
    layers = [_FakeLayer(index) for index in range(42)]
    _FakeProjectApi.current = _FakeProject(layers)
    iface = _FakeIface(layers[-1])
    selected = [{
        "name": "selected_roads",
        "kind": "vector line",
        "crs": "EPSG:32651",
        "feature_count": 7,
    }]

    normal = snapshot_module.build_context_block(
        iface, selected_layers=selected)
    assert "Layers (42):" in normal
    assert "Canvas extent (EPSG:32651): 1.2000, 2.6000, 4.1000, 5.8000" in normal
    assert "7 features" in normal
    assert all(layer.count_calls == 1 for layer in layers)

    fast = snapshot_module.build_context_block(
        iface, selected_layers=selected, fast_mode=True)
    assert "Layers (42; showing up to 30; counts omitted — fast mode):" in fast
    assert fast.count("\n  - layer_") == 30
    assert "\n  - layer_29 " in fast
    assert "\n  - layer_30 " not in fast
    assert "Canvas extent (EPSG:32651): 1, 3, 4, 6" in fast
    assert "7 features" not in fast
    assert all(layer.count_calls == 1 for layer in layers)
    assert snapshot_module.build_fast_mode_directive() in fast
    assert snapshot_module.build_fast_mode_directive() not in normal


def test_selected_snapshot_can_skip_feature_count(snapshot_module):
    layer = _FakeLayer(0)

    fast_snapshot = snapshot_module.snapshot_layer(
        layer, include_feature_count=False)
    assert fast_snapshot["feature_count"] is None
    assert layer.count_calls == 0

    normal_snapshot = snapshot_module.snapshot_layer(layer)
    assert normal_snapshot["feature_count"] == 1
    assert layer.count_calls == 1


def test_fast_mode_directive_matches_goal_contract(snapshot_module):
    assert snapshot_module.build_fast_mode_directive() == (
        "## FAST MODE (user-enabled for this turn)\n"
        "Work with the minimum number of model round trips:\n"
        "- Do the work yourself in ONE consolidated execute_pyqgis script where\n"
        "  feasible. Do NOT delegate to subagents and do NOT dispatch the\n"
        "  qa-verifier (Claude backend); skip the separate self-verification\n"
        "  pass (Codex backend).\n"
        "- Still report evidence inline: print created layer names, feature\n"
        "  counts, CRS, and use stat_path for any exported file. Evidence stays;\n"
        "  the extra verification TURN goes.\n"
        "- Skip render_map_snapshot unless the user explicitly asked to see the\n"
        "  result.\n"
        "- Keep prose terse: answer, evidence, done. No restated plans for\n"
        "  simple tasks.\n"
        "- UNCHANGED even in fast mode: destructive-code approval, asking via\n"
        "  ask_user when ambiguity materially changes the outcome (a wrong\n"
        "  guess costs more than the question), and honest failure reporting."
    )


def test_codex_effort_override_is_scoped_to_fast_fresh_and_resume(
        backend_loader, tmp_path):
    module, _config = backend_loader("codex_backend")
    mcp_path = tmp_path / "mcp.json"
    mcp_path.write_text(json.dumps({
        "mcpServers": {
            "qgis": {
                "command": "python",
                "args": ["bridge.py"],
                "env": {},
            },
        },
    }), encoding="utf-8")
    backend = module.CodexBackend(
        "codex", str(tmp_path), str(mcp_path), {})

    backend.send("fresh fast", "", fast_mode=True)
    backend.proc = None
    backend.session_id = "thread-123"
    backend.send("resumed fast", "", fast_mode=True)
    backend.proc = None
    backend.send("resumed normal", "", fast_mode=False)

    fresh_fast = _FakeProcess.starts[0][1]
    resumed_fast = _FakeProcess.starts[1][1]
    resumed_normal = _FakeProcess.starts[2][1]
    effort_pair = ["-c", 'model_reasoning_effort="low"']
    assert fresh_fast[0] == "exec"
    assert resumed_fast[:2] == ["exec", "resume"]
    assert any(
        fresh_fast[index:index + 2] == effort_pair
        for index in range(len(fresh_fast) - 1))
    assert any(
        resumed_fast[index:index + 2] == effort_pair
        for index in range(len(resumed_fast) - 1))
    assert not any(
        resumed_normal[index:index + 2] == effort_pair
        for index in range(len(resumed_normal) - 1))


def test_claude_effort_override_survives_fallback_but_not_normal_turn(
        backend_loader, tmp_path):
    module, config_module = backend_loader("claude_code_backend")
    backend = module.ClaudeCodeBackend(
        "claude", str(tmp_path), "mcp.json", {})

    backend.send("fast", "", fast_mode=True)
    first = backend.proc
    backend._retire_process(first)
    backend._start_attempt("opus", None)
    fallback = backend.proc
    backend._retire_process(fallback)

    config_module.model_id["claude"] = "opus"
    backend._clear_turn_state()
    backend.send("normal", "", fast_mode=False)

    assert ["--effort", "low"] == _FakeProcess.starts[0][1][
        _FakeProcess.starts[0][1].index("--effort"):
        _FakeProcess.starts[0][1].index("--effort") + 2]
    assert ["--effort", "low"] == _FakeProcess.starts[1][1][
        _FakeProcess.starts[1][1].index("--effort"):
        _FakeProcess.starts[1][1].index("--effort") + 2]
    assert "--effort" not in _FakeProcess.starts[2][1]
