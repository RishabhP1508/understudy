"""The five invariants. Fixed and external — never edit this file.

Each test enforces one invariant by walking real ASTs and real schemas, not by grepping prose.
Where the guarded code does not exist yet, the test skips loudly, naming the phase that supplies it.
"""

from __future__ import annotations

import ast
import base64
import importlib.util
import json
import re
import urllib.parse
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
UNDERSTUDY_ROOT = SRC / "understudy"
ARTIFACTS_DIR = Path(__file__).resolve().parent.parent / "artifacts"

_BANNED_MODULE_PREFIXES = (
    "understudy.llm",
    "google.genai",
    "google.generativeai",
    "openai",
    "anthropic",
)
_BANNED_SCHEMA_KEYS = {"messages", "transcript", "completion", "choices", "content"}


# --------------------------------------------------------------------------------------
# shared helpers (all module-private; none of these may start with "test_")
# --------------------------------------------------------------------------------------


def _module_name_for_file(path: Path) -> str:
    rel = path.relative_to(SRC).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _first_party_module_map(root: Path) -> dict[str, Path]:
    return {_module_name_for_file(p): p for p in root.rglob("*.py")}


def _is_banned_module(module_name: str) -> bool:
    return any(
        module_name == prefix or module_name.startswith(prefix + ".")
        for prefix in _BANNED_MODULE_PREFIXES
    )


def _collect_module_imports(tree: ast.AST, module_name: str, is_package: bool) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                package = module_name if is_package else module_name.rsplit(".", 1)[0]
                relative = "." * node.level + (node.module or "")
                try:
                    resolved = importlib.util.resolve_name(relative, package)
                except ImportError:
                    continue
            else:
                resolved = node.module or ""
            if resolved:
                names.add(resolved)
                for alias in node.names:
                    names.add(f"{resolved}.{alias.name}")
    return names


def _reachable_modules(start_files: list[Path], module_map: dict[str, Path]) -> set[str]:
    """BFS over first-party imports starting from start_files; returns every module name reached."""
    reached: set[str] = set()
    visited_files: set[Path] = set()
    queue = list(start_files)
    while queue:
        path = queue.pop()
        if path in visited_files:
            continue
        visited_files.add(path)
        module_name = _module_name_for_file(path)
        is_package = path.name == "__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name in _collect_module_imports(tree, module_name, is_package):
            reached.add(name)
            target = module_map.get(name)
            if target is not None and target not in visited_files:
                queue.append(target)
    return reached


class _ActCallVisitor(ast.NodeVisitor):
    """Records the (enclosing class, enclosing function) of every `<expr>.act(...)` call site."""

    def __init__(self) -> None:
        self.class_stack: list[str] = []
        self.func_stack: list[str] = []
        self.hits: set[tuple[str | None, str | None]] = set()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_func(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_func(node)

    def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.func_stack.append(node.name)
        self.generic_visit(node)
        self.func_stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        # Matching on the attribute name alone is deliberately over-broad (any `.act(...)`
        # anywhere counts, not just Surface.act); the codebase must live with that breadth.
        if isinstance(node.func, ast.Attribute) and node.func.attr == "act":
            cls = self.class_stack[-1] if self.class_stack else None
            fn = self.func_stack[-1] if self.func_stack else None
            self.hits.add((cls, fn))
        self.generic_visit(node)


def _has_policy_gate_dispatch(root: Path) -> bool:
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "PolicyGate":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                        if item.name == "dispatch":
                            return True
    return False


def _collect_schema_property_names(schema: dict) -> set[str]:
    names: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                names.update(props.keys())
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return names


def _resolve_schema_ref(schema: dict, ref: str) -> dict:
    assert ref.startswith("#/"), f"unsupported $ref shape: {ref}"
    node: dict = schema
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _schema_has_path(schema: dict, path: list[str]) -> bool:
    node = schema
    for key in path:
        props = node.get("properties", {})
        if key not in props:
            return False
        node = props[key]
        if "$ref" in node:
            node = _resolve_schema_ref(schema, node["$ref"])
        elif "allOf" in node and len(node["allOf"]) == 1 and "$ref" in node["allOf"][0]:
            node = _resolve_schema_ref(schema, node["allOf"][0]["$ref"])
    return True


def _collect_dict_keys(data: object) -> set[str]:
    keys: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            keys.update(node.keys())
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return keys


def _build_parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _enclosing_finish_node(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST:
    """Nearest enclosing If/match_case, falling back to the nearest enclosing function."""
    current: ast.AST = node
    while current in parents:
        current = parents[current]
        if isinstance(current, ast.If | ast.match_case):
            return current
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
            return current
    raise AssertionError("the 'finish' constant is not enclosed by any function in loop.py")


def _is_verify_checkpoint_call(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "verify_checkpoint"
    if isinstance(func, ast.Attribute):
        return func.attr == "verify_checkpoint"
    return False


def _assert_verifies_before_returning(node: ast.AST) -> None:
    calls = [c for c in ast.walk(node) if isinstance(c, ast.Call) and _is_verify_checkpoint_call(c)]
    assert calls, "finish handling in loop.py never calls verify_checkpoint"
    first_call_lineno = min(c.lineno for c in calls)
    early_returns = [
        r for r in ast.walk(node) if isinstance(r, ast.Return) and r.lineno < first_call_lineno
    ]
    assert not early_returns, "loop.py returns before verifying the checkpoint"


# --------------------------------------------------------------------------------------
# the five invariants
# --------------------------------------------------------------------------------------


def test_no_model_in_replay_path() -> None:
    """Invariant 1: nothing importable from src/understudy/replay/ transitively imports an LLM."""
    replay_dir = UNDERSTUDY_ROOT / "replay"
    py_files = list(replay_dir.rglob("*.py")) if replay_dir.exists() else []
    non_init_files = [p for p in py_files if p.name != "__init__.py"]
    if not non_init_files:
        pytest.skip(
            "src/understudy/replay/ arrives in Phase 2; invariant 1 goes live then"
        )
    module_map = _first_party_module_map(UNDERSTUDY_ROOT)
    reached = _reachable_modules(py_files, module_map)
    banned_hits = {name for name in reached if _is_banned_module(name)}
    assert not banned_hits, f"replay/ transitively imports banned module(s): {sorted(banned_hits)}"


def test_every_action_passes_the_policy_gate() -> None:
    """Invariant 2: Surface.act is only ever called from PolicyGate.dispatch."""
    if not _has_policy_gate_dispatch(SRC):
        pytest.skip(
            "src/understudy/safety/policy.py (PolicyGate.dispatch) arrives in Phase 2; "
            "invariant 2 goes live then"
        )
    visitor = _ActCallVisitor()
    for path in UNDERSTUDY_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor.visit(tree)
    assert visitor.hits == {("PolicyGate", "dispatch")}


def test_nothing_sensitive_is_serialized() -> None:
    """Invariant 3: no sentinel secret survives dumps(), in utf-8, url-encoded, or base64 form."""
    redact_path = UNDERSTUDY_ROOT / "safety" / "redact.py"
    if not redact_path.exists():
        pytest.skip(
            "understudy.safety.redact.Redactor arrives in Phase 2 (stub) and Phase 5 (full); "
            "invariant 3 goes live then"
        )
    from understudy.safety.redact import Redactor
    payload = {
        "note": "SECRET_SENTINEL_VALUE",
        "customer": {"ssn": "123-45-6789", "items": ["ok", "SECRET_SENTINEL_VALUE"]},
    }
    output = Redactor().dumps(payload)
    if isinstance(output, bytes):
        output = output.decode("utf-8")

    for secret in ("SECRET_SENTINEL_VALUE", "123-45-6789"):
        assert secret not in output, f"raw sentinel {secret!r} leaked into serialized output"
        assert urllib.parse.quote(secret) not in output
        assert urllib.parse.quote_plus(secret) not in output
        for pad in (b"", b"x", b"xx"):
            encoded = base64.b64encode(pad + secret.encode()).rstrip(b"=")
            start = {0: 0, 1: 2, 2: 3}[len(pad)]
            substring = encoded[start:-2]
            if substring:
                assert substring.decode("ascii") not in output, (
                    f"base64-encoded sentinel {secret!r} (pad={len(pad)}) leaked into output"
                )


def test_artifact_is_decoupled_from_transcript() -> None:
    """Invariant 4: a serialized Capability has no transcript-shaped key, only transcript_hash."""
    artifact_module_path = UNDERSTUDY_ROOT / "models" / "artifact.py"
    if not artifact_module_path.exists():
        pytest.skip("understudy.models.artifact.Capability arrives in Phase 2; invariant 4 then")
    from understudy.models.artifact import Capability

    schema = Capability.model_json_schema()
    property_names = {name.lower() for name in _collect_schema_property_names(schema)}
    # Exact match only: "transcript_hash" is allowed by design, it is not "transcript".
    banned_hits = _BANNED_SCHEMA_KEYS & property_names
    assert not banned_hits, f"Capability schema exposes banned key(s): {banned_hits}"
    assert _schema_has_path(schema, ["provenance", "transcript_hash"]), (
        "Capability schema must declare provenance.transcript_hash"
    )

    for artifact_path in sorted(ARTIFACTS_DIR.glob("*.json")):
        data = json.loads(artifact_path.read_text(encoding="utf-8"))
        found_keys = {key.lower() for key in _collect_dict_keys(data)}
        banned_hits = _BANNED_SCHEMA_KEYS & found_keys
        assert not banned_hits, f"{artifact_path}: banned key(s) present: {banned_hits}"
        transcript_hash = data.get("provenance", {}).get("transcript_hash")
        assert transcript_hash is not None, f"{artifact_path}: missing provenance.transcript_hash"
        assert re.fullmatch(r"[0-9a-f]{32,128}", transcript_hash), (
            f"{artifact_path}: provenance.transcript_hash is not a hex digest: {transcript_hash!r}"
        )


def test_model_never_self_declares_success() -> None:
    """Invariant 5: finish requires a checkpoint, and the loop verifies it before returning."""
    tools_path = UNDERSTUDY_ROOT / "agent" / "tools.py"
    if not tools_path.exists():
        pytest.skip("understudy.agent.tools.FINISH_TOOL arrives in Phase 2; invariant 5 then")
    from understudy.agent.tools import FINISH_TOOL
    assert "checkpoint" in FINISH_TOOL["parameters"]["required"]

    loop_path = UNDERSTUDY_ROOT / "agent" / "loop.py"
    if not loop_path.exists():
        pytest.skip("src/understudy/agent/loop.py arrives in Phase 2; invariant 5 goes live then")

    tree = ast.parse(loop_path.read_text(encoding="utf-8"), filename=str(loop_path))
    parents = _build_parent_map(tree)
    finish_constants = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value == "finish"
    ]
    assert finish_constants, "loop.py has no 'finish' string constant to anchor the check on"
    for constant in finish_constants:
        enclosing = _enclosing_finish_node(constant, parents)
        _assert_verifies_before_returning(enclosing)
