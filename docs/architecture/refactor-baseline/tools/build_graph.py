"""Mechanical dependency-graph generator for the Py4GW_Reforged refactor baseline.

This tool walks the in-scope Python of the Py4GW_Reforged repository and emits a single
``graph.json`` describing the static import structure. Every number quoted in the
refactor-baseline documents comes from this file's output.

Scope is fixed in :data:`IN_SCOPE_ROOTS` / :data:`EXCLUDED_DIR_NAMES` so the output is
reproducible: the sibling ``Py4GW_Reforged_Native`` repository, ``Assets/``, ``stubs/`` and
``docs/`` are never walked. Imports of the C++ binding surface (the modules declared by
``stubs/*.pyi``) are recorded as native-boundary crossings and never followed.

What this tool can and cannot prove is emitted in ``meta.limitations`` and must be carried
into any document built on this data. In particular, widgets are discovered at runtime by
``os.walk`` + ``importlib.util.spec_from_file_location`` in
``Py4GWCoreLib/py4gwcorelib_src/WidgetManager.py``; no static import edge points at them, so
zero fan-in on a widget is *not* evidence of unreachability.

Usage::

    python docs/architecture/refactor-baseline/tools/build_graph.py
    python docs/architecture/refactor-baseline/tools/build_graph.py --repo-root . --out graph.json
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------------------
# Scope
# --------------------------------------------------------------------------------------

#: Top-level directories walked by this tool.
IN_SCOPE_ROOTS: tuple[str, ...] = (
    "Py4GWCoreLib",
    "Widgets",
    "Bots",
    "py4gw_bridge",
    "BridgeRuntime",
    "Py4GW_Reforged_Launcher",
    "Sources",
    "Examples and tests",
)

#: Root-level ``*.py`` entry points are in scope; they bind the widget/script host layer.
INCLUDE_ROOT_LEVEL_SCRIPTS: bool = True

#: Directory names never descended into, at any depth.
EXCLUDED_DIR_NAMES: frozenset[str] = frozenset(
    {
        "__pycache__",
        ".git",
        ".vs",
        ".venv",
        "venv",
        "node_modules",
        "Assets",
        "stubs",
        "docs",
        "textures",
        "build",
        "dist",
        ".mypy_cache",
        ".pytest_cache",
    }
)

#: Data roots whose files are treated as configuration/data coupling.
DATA_ROOTS: tuple[str, ...] = ("json", "Settings", "data", "offsets")

#: Areas whose ownership sits one level below the area root.
NAMESPACE_SPLIT_AREAS: frozenset[str] = frozenset(
    {"Sources", "Widgets", "Bots", "Examples and tests", "Py4GWCoreLib"}
)

#: Owned persistence entry points. Data coupling in this repository is expressed almost
#: entirely as a *logical document name* passed to one of these, not as a rooted path
#: literal, so these call sites are the real coupling signal into json/ and Settings/.
PERSISTENCE_CALLS: frozenset[str] = frozenset(
    {"JsonFactory", "Settings", "IniHandler", "IniManager", "get_json_factory", "get_settings"}
)

#: Unjailed file access. PF-3 flags these as a defect class when they bypass the owners
#: above; counting them per namespace is what makes that claim measurable.
RAW_IO_CALLS: frozenset[str] = frozenset(
    {"open", "makedirs", "mkdir", "read_text", "write_text", "unlink", "remove", "rmtree"}
)

#: ``json.load`` / ``json.dump`` style attribute calls, matched on the attribute name.
RAW_JSON_CALLS: frozenset[str] = frozenset({"load", "dump", "loads", "dumps"})

#: Top-level names that were importable roots historically. ``HeroAI`` now lives at
#: ``Py4GWCoreLib/HeroAI``; the root directory survives only as an empty husk, so a bare
#: ``HeroAI.*`` import is broken rather than external.
LEGACY_TOP_LEVELS: frozenset[str] = frozenset({"HeroAI", "BridgeRuntime"})

# --------------------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------------------

_DOTTED_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DATA_REF_RE = re.compile(r"((?:" + "|".join(DATA_ROOTS) + r")/[^\"'\s]*)")
_LOOSE_DATA_FILE_RE = re.compile(r"^[^\s\"']*\.(?:json|ini)$", re.IGNORECASE)


# --------------------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------------------


@dataclass
class RawImport:
    """A statically detected module reference, before resolution.

    ``path_target`` is set for relative imports, which are resolved against the file tree
    rather than a dotted name. Much of this repository lives in directories whose names are
    not valid identifiers (``Bots/Example Bots/YAVB/``), so those packages have no dotted
    name at all -- but ``from .FSM import ...`` inside them still resolves at runtime,
    because the loader works from the file's own location.
    """

    target: str
    kind: str
    line: int
    path_target: str | None = None


@dataclass
class DynamicSite:
    """A runtime-dispatch site the static graph cannot follow."""

    kind: str
    line: int
    detail: str


@dataclass
class ModuleInfo:
    """Everything the walker knows about one in-scope Python file."""

    path: str
    area: str
    namespace: str
    dotted: str | None
    is_package_init: bool
    loc: int
    parse_error: str | None = None
    raw_imports: list[RawImport] = field(default_factory=list["RawImport"])
    string_refs: list[RawImport] = field(default_factory=list["RawImport"])
    dynamic_sites: list[DynamicSite] = field(default_factory=list["DynamicSite"])
    data_refs: set[str] = field(default_factory=set[str])
    loose_data_literals: set[str] = field(default_factory=set[str])
    native_imports: set[str] = field(default_factory=set[str])
    imports: set[str] = field(default_factory=set[str])
    importers: set[str] = field(default_factory=set[str])
    unresolved: list[dict[str, Any]] = field(default_factory=list["dict[str, Any]"])
    persistence_sites: list[dict[str, Any]] = field(default_factory=list["dict[str, Any]"])
    raw_io_sites: list[dict[str, Any]] = field(default_factory=list["dict[str, Any]"])


# --------------------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------------------


def _is_excluded(rel_parts: tuple[str, ...]) -> bool:
    return any(part in EXCLUDED_DIR_NAMES for part in rel_parts)


def discover_python_files(repo_root: Path) -> list[Path]:
    """Return every in-scope ``*.py`` file, sorted for deterministic output."""

    found: set[Path] = set()
    for root_name in IN_SCOPE_ROOTS:
        root_path = repo_root / root_name
        if not root_path.is_dir():
            continue
        for candidate in root_path.rglob("*.py"):
            rel = candidate.relative_to(repo_root)
            if _is_excluded(rel.parts[:-1]):
                continue
            found.add(candidate)
    if INCLUDE_ROOT_LEVEL_SCRIPTS:
        for candidate in repo_root.glob("*.py"):
            found.add(candidate)
    return sorted(found)


def native_module_names(repo_root: Path) -> set[str]:
    """Names of the C++ binding modules, taken from ``stubs/*.pyi``."""

    stub_dir = repo_root / "stubs"
    if not stub_dir.is_dir():
        return set()
    return {stub.stem for stub in stub_dir.glob("*.pyi")}


def _area_for(rel_posix: str) -> str:
    parts = rel_posix.split("/")
    if len(parts) == 1:
        return "Root"
    return parts[0]


def _namespace_for(rel_posix: str, area: str) -> str:
    if area == "Root":
        return "Root"
    parts = rel_posix.split("/")
    if area in NAMESPACE_SPLIT_AREAS and len(parts) > 2:
        return parts[0] + "/" + parts[1]
    return area


def _dotted_for(rel_posix: str) -> str | None:
    """Dotted module name for a path, or ``None`` when the path is not importable."""

    parts = rel_posix.split("/")
    stem = parts[-1][: -len(".py")]
    chain = parts[:-1] + ([] if stem == "__init__" else [stem])
    if not chain:
        return None
    for part in chain:
        if _IDENT_RE.match(part) is None:
            return None
    return ".".join(chain)


# --------------------------------------------------------------------------------------
# AST walking
# --------------------------------------------------------------------------------------


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _first_str_arg(node: ast.Call) -> str | None:
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _relative_base(path: str, level: int) -> str | None:
    """Directory a relative import of ``level`` dots resolves against.

    ``level == 1`` is the containing package. For both ``pkg/mod.py`` and
    ``pkg/__init__.py`` that is ``pkg``, so the two cases need no special handling.
    Returns ``None`` when the import walks above the repository root.
    """

    parts = path.split("/")[:-1]
    up = level - 1
    if up > len(parts):
        return None
    if up:
        parts = parts[: len(parts) - up]
    return "/".join(parts)


class _ModuleVisitor(ast.NodeVisitor):
    """Collects import, dynamic-dispatch and data-reference evidence from one module."""

    def __init__(self, info: ModuleInfo) -> None:
        self.info = info

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.info.raw_imports.append(RawImport(target=alias.name, kind="import", line=node.lineno))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        level = node.level
        if level > 0:
            base = _relative_base(self.info.path, level)
            if base is None:
                self.info.unresolved.append(
                    {
                        "target": ("." * level) + (node.module or ""),
                        "kind": "relative",
                        "line": node.lineno,
                        "reason": "unanchored-relative",
                    }
                )
            else:
                prefix = base
                if node.module:
                    suffix = node.module.replace(".", "/")
                    prefix = suffix if not base else base + "/" + suffix
                # Always emit the package/module edge, including for bare ``from . import X``:
                # importing a symbol still executes the package __init__.
                if prefix:
                    self.info.raw_imports.append(
                        RawImport(target=prefix, kind="relative", line=node.lineno, path_target=prefix)
                    )
                for alias in node.names:
                    if alias.name != "*":
                        member = alias.name if not prefix else prefix + "/" + alias.name
                        self.info.raw_imports.append(
                            RawImport(
                                target=member,
                                kind="relative-member",
                                line=node.lineno,
                                path_target=member,
                            )
                        )
        elif node.module:
            self.info.raw_imports.append(RawImport(target=node.module, kind="from", line=node.lineno))
            for alias in node.names:
                if alias.name != "*":
                    self.info.raw_imports.append(
                        RawImport(
                            target=node.module + "." + alias.name,
                            kind="from-member",
                            line=node.lineno,
                        )
                    )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node)
        if name in ("import_module", "__import__"):
            arg = _first_str_arg(node)
            if arg is not None:
                self.info.raw_imports.append(RawImport(target=arg, kind="importlib", line=node.lineno))
            else:
                self.info.dynamic_sites.append(
                    DynamicSite(kind="importlib-dynamic", line=node.lineno, detail=name + "(<non-literal>)")
                )
        elif name in ("spec_from_file_location", "module_from_spec", "SourceFileLoader"):
            self.info.dynamic_sites.append(DynamicSite(kind="path-loader", line=node.lineno, detail=name))
        elif name in ("exec", "eval") and node.args:
            self.info.dynamic_sites.append(DynamicSite(kind="exec", line=node.lineno, detail=name))
        elif name in ("append", "insert") and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if (
                isinstance(owner, ast.Attribute)
                and owner.attr == "path"
                and isinstance(owner.value, ast.Name)
                and owner.value.id == "sys"
            ):
                self.info.dynamic_sites.append(
                    DynamicSite(kind="sys-path-mutation", line=node.lineno, detail="sys.path." + name)
                )

        if name in PERSISTENCE_CALLS:
            self.info.persistence_sites.append(
                {"owner": name, "document": _first_str_arg(node), "line": node.lineno}
            )
        elif name in RAW_JSON_CALLS and isinstance(node.func, ast.Attribute):
            base = node.func.value
            if isinstance(base, ast.Name) and base.id == "json":
                self.info.raw_io_sites.append({"call": "json." + name, "line": node.lineno})
        elif name in RAW_IO_CALLS:
            self.info.raw_io_sites.append({"call": name, "line": node.lineno})

        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        value = node.value
        if isinstance(value, str) and value:
            self._scan_string(value)
        self.generic_visit(node)

    def _scan_string(self, value: str) -> None:
        if len(value) > 400 or "\n" in value:
            return
        # A bare dotted string that names a real in-repo module is a config-driven
        # dispatch target (e.g. a (label, module_path) table fed to import_module later).
        # Recorded as a weak 'string-ref' edge so it is visible but distinguishable.
        if "." in value and " " not in value and _DOTTED_RE.match(value) is not None:
            self.info.string_refs.append(RawImport(target=value, kind="string-ref", line=0))
        normalized = value.replace("\\", "/")
        match = _DATA_REF_RE.search(normalized)
        if match is not None:
            self.info.data_refs.add(match.group(1))
        elif "/" not in normalized and _LOOSE_DATA_FILE_RE.match(normalized) is not None:
            self.info.loose_data_literals.add(normalized)


def parse_module(repo_root: Path, file_path: Path) -> ModuleInfo:
    """Parse one file into a :class:`ModuleInfo`, tolerating unreadable or invalid source."""

    rel_posix = file_path.relative_to(repo_root).as_posix()
    area = _area_for(rel_posix)
    namespace = _namespace_for(rel_posix, area)
    dotted = _dotted_for(rel_posix)
    is_init = file_path.name == "__init__.py"
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ModuleInfo(
            path=rel_posix,
            area=area,
            namespace=namespace,
            dotted=dotted,
            is_package_init=is_init,
            loc=0,
            parse_error="read-error: " + str(exc),
        )
    info = ModuleInfo(
        path=rel_posix,
        area=area,
        namespace=namespace,
        dotted=dotted,
        is_package_init=is_init,
        loc=source.count("\n") + 1,
    )
    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError as exc:
        info.parse_error = "syntax-error: line " + str(exc.lineno) + ": " + str(exc.msg)
        return info
    except ValueError as exc:
        info.parse_error = "value-error: " + str(exc)
        return info
    _ModuleVisitor(info).visit(tree)
    return info


# --------------------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------------------


class Resolver:
    """Maps dotted module references onto in-scope file paths."""

    def __init__(self, modules: dict[str, ModuleInfo], natives: set[str]) -> None:
        self.modules = modules
        self.natives = natives
        self.by_dotted: dict[str, str] = {}
        self.by_leaf: dict[str, set[str]] = {}
        self.by_dir_leaf: dict[tuple[str, str], str] = {}
        #: Top-level names that name something inside this repository. A dotted target whose
        #: head is one of these but which does not resolve is a broken in-repo import, not an
        #: external package -- guessing a leaf match for it would manufacture a false edge.
        self.inrepo_heads: set[str] = set(IN_SCOPE_ROOTS) | set(LEGACY_TOP_LEVELS)
        #: Directories that contain Python and whose path segments are all identifiers. Most
        #: of this repository ships without ``__init__.py``, so these are importable implicit
        #: namespace packages; a reference to one is valid even though no file backs it.
        self.packages: set[str] = set()
        #: The same directories keyed by path, for relative-import resolution. Unlike
        #: ``packages`` this has no identifier constraint, because a relative import does
        #: not need its directory to be a legal dotted name.
        self.package_dirs: set[str] = set()
        for path in modules:
            segments = path.split("/")[:-1]
            for depth in range(1, len(segments) + 1):
                chain = segments[:depth]
                self.package_dirs.add("/".join(chain))
                if all(_IDENT_RE.match(part) is not None for part in chain):
                    self.packages.add(".".join(chain))
        for path, info in modules.items():
            if info.dotted is not None and info.dotted not in self.by_dotted:
                self.by_dotted[info.dotted] = path
            if info.dotted is not None:
                self.inrepo_heads.add(info.dotted.split(".")[0])
            leaf = path.rsplit("/", 1)[-1][: -len(".py")]
            self.by_leaf.setdefault(leaf, set()).add(path)
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            key = (parent, "__package__" if leaf == "__init__" else leaf)
            self.by_dir_leaf.setdefault(key, path)

    def resolve_path(self, path_target: str) -> tuple[str | None, str]:
        """Resolve a relative import against the file tree.

        Tries the module file, then the package ``__init__``, then treats a directory that
        contains Python as an implicit namespace package.
        """

        as_module = path_target + ".py"
        if as_module in self.modules:
            return as_module, "relative-file"
        as_package = path_target + "/__init__.py"
        if as_package in self.modules:
            return as_package, "relative-package"
        if path_target in self.package_dirs:
            return None, "namespace-package"
        return None, "relative-missing"

    def resolve(self, source_path: str, target: str) -> tuple[str | None, str]:
        """Return ``(resolved_path, reason)``; ``resolved_path`` is ``None`` when unresolved."""

        if _DOTTED_RE.match(target) is None:
            return None, "not-a-module-path"
        head = target.split(".")[0]
        if head in self.natives:
            return None, "native-boundary"
        if head in sys.stdlib_module_names:
            return None, "stdlib"

        exact = self.by_dotted.get(target)
        if exact is not None:
            return exact, "exact"

        if target in self.packages:
            return None, "namespace-package"

        if "." in target:
            parent = target.rsplit(".", 1)[0]
            parent_hit = self.by_dotted.get(parent)
            if parent_hit is not None:
                return parent_hit, "member-of"
            # A dotted target rooted in this repository that resolved neither exactly nor via
            # its parent package is broken, not external. Reporting it beats guessing a leaf.
            if head in self.inrepo_heads:
                return None, "broken-inrepo-import"

        if "." not in target:
            source_dir = source_path.rsplit("/", 1)[0] if "/" in source_path else ""
            sibling = self.by_dir_leaf.get((source_dir, target))
            if sibling is not None and sibling != source_path:
                return sibling, "sibling"

        leaf = target.rsplit(".", 1)[-1]
        candidates = self.by_leaf.get(leaf, set()) - {source_path}
        if len(candidates) == 1:
            return next(iter(candidates)), "unique-leaf"
        if len(candidates) > 1:
            return None, "ambiguous-leaf"
        return None, "external-or-missing"


# --------------------------------------------------------------------------------------
# Graph analysis
# --------------------------------------------------------------------------------------


def find_cycles(adjacency: dict[str, set[str]]) -> list[list[str]]:
    """Iterative Tarjan SCC; returns non-trivial strongly connected components."""

    index_of: dict[str, int] = {}
    low_of: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    result: list[list[str]] = []
    counter = 0

    for root in sorted(adjacency):
        if root in index_of:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, child_index = work[-1]
            if child_index == 0:
                index_of[node] = counter
                low_of[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            children = sorted(adjacency.get(node, set()))
            if child_index < len(children):
                work[-1] = (node, child_index + 1)
                child = children[child_index]
                if child not in index_of:
                    work.append((child, 0))
                elif child in on_stack:
                    low_of[node] = min(low_of[node], index_of[child])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low_of[parent] = min(low_of[parent], low_of[node])
            if low_of[node] == index_of[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    result.append(sorted(component))
    result.sort(key=lambda comp: (-len(comp), comp[0]))
    return result


def _self_loops(adjacency: dict[str, set[str]]) -> list[str]:
    return sorted(node for node, targets in adjacency.items() if node in targets)


# --------------------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------------------


def _new_bucket() -> dict[str, Any]:
    return {
        "modules": 0,
        "loc": 0,
        "parse_errors": 0,
        "internal_edges": 0,
        "inbound_edges": 0,
        "outbound_edges": 0,
        "native_imports": set(),
        "data_refs": set(),
    }


def build_graph(repo_root: Path) -> dict[str, Any]:
    """Walk the repository and return the full graph document."""

    files = discover_python_files(repo_root)
    natives = native_module_names(repo_root)
    modules: dict[str, ModuleInfo] = {}
    for file_path in files:
        info = parse_module(repo_root, file_path)
        modules[info.path] = info

    resolver = Resolver(modules, natives)
    edges: list[dict[str, Any]] = []
    unresolved_counts: dict[str, int] = {}

    for path in sorted(modules):
        info = modules[path]
        # One edge per (source, target) pair. A single ``from X import A, B`` yields several
        # references to the same module; counting each as an edge would inflate every fan-in
        # figure in the register.
        edge_index: dict[str, int] = {}
        for raw in info.raw_imports + info.string_refs:
            if raw.path_target is not None:
                resolved, reason = resolver.resolve_path(raw.path_target)
            else:
                head = raw.target.split(".")[0]
                if head in natives:
                    info.native_imports.add(head)
                    continue
                resolved, reason = resolver.resolve(path, raw.target)
            if resolved is None:
                unresolved_counts[reason] = unresolved_counts.get(reason, 0) + 1
                # A string that merely looks like a module path is not evidence of a broken
                # import -- prose mentions module names constantly. Only real import
                # statements are reported as breakage.
                if raw.kind == "string-ref":
                    continue
                if reason in ("ambiguous-leaf", "unanchored-relative", "broken-inrepo-import"):
                    info.unresolved.append(
                        {"target": raw.target, "kind": raw.kind, "line": raw.line, "reason": reason}
                    )
                continue
            if resolved == path:
                continue
            # A string is only evidence of a dispatch target when it names a module exactly.
            # Allowing the leaf/sibling heuristics here turns any bare word that happens to
            # match a filename into an edge.
            if raw.kind == "string-ref" and reason != "exact":
                continue
            existing = edge_index.get(resolved)
            if existing is not None:
                kinds: list[str] = edges[existing]["kinds"]
                if raw.kind not in kinds:
                    kinds.append(raw.kind)
                continue
            edge_index[resolved] = len(edges)
            info.imports.add(resolved)
            modules[resolved].importers.add(path)
            edges.append(
                {
                    "source": path,
                    "target": resolved,
                    "kinds": [raw.kind],
                    "line": raw.line,
                    "resolution": reason,
                }
            )

    adjacency: dict[str, set[str]] = {path: set(info.imports) for path, info in modules.items()}
    cycles = find_cycles(adjacency)
    cycle_members: set[str] = {member for cycle in cycles for member in cycle}

    module_docs: dict[str, Any] = {}
    for path in sorted(modules):
        info = modules[path]
        external_importers = sorted(p for p in info.importers if modules[p].namespace != info.namespace)
        cross_area_importers = sorted(p for p in info.importers if modules[p].area != info.area)
        module_docs[path] = {
            "path": path,
            "area": info.area,
            "namespace": info.namespace,
            "dotted": info.dotted,
            "loc": info.loc,
            "parse_error": info.parse_error,
            "fan_in": len(info.importers),
            "fan_out": len(info.imports),
            "fan_in_external": len(external_importers),
            "fan_in_cross_area": len(cross_area_importers),
            "importers": sorted(info.importers),
            "external_importers": external_importers,
            "imports": sorted(info.imports),
            "native_imports": sorted(info.native_imports),
            "data_refs": sorted(info.data_refs),
            "loose_data_literals": sorted(info.loose_data_literals),
            "persistence_sites": info.persistence_sites,
            "raw_io_sites": info.raw_io_sites,
            "raw_io_count": len(info.raw_io_sites),
            "dynamic_sites": [
                {"kind": site.kind, "line": site.line, "detail": site.detail} for site in info.dynamic_sites
            ],
            "unresolved_imports": info.unresolved,
            "in_cycle": path in cycle_members,
        }

    areas: dict[str, dict[str, Any]] = {}
    namespaces: dict[str, dict[str, Any]] = {}
    for path in sorted(modules):
        info = modules[path]
        for bucket, key in ((areas, info.area), (namespaces, info.namespace)):
            entry = bucket.setdefault(key, _new_bucket())
            entry["modules"] = int(entry["modules"]) + 1
            entry["loc"] = int(entry["loc"]) + info.loc
            if info.parse_error is not None:
                entry["parse_errors"] = int(entry["parse_errors"]) + 1
            natives_seen: set[str] = entry["native_imports"]
            natives_seen.update(info.native_imports)
            refs_seen: set[str] = entry["data_refs"]
            refs_seen.update(info.data_refs)

    for edge in edges:
        src = modules[str(edge["source"])]
        dst = modules[str(edge["target"])]
        if src.area == dst.area:
            areas[src.area]["internal_edges"] = int(areas[src.area]["internal_edges"]) + 1
        else:
            areas[src.area]["outbound_edges"] = int(areas[src.area]["outbound_edges"]) + 1
            areas[dst.area]["inbound_edges"] = int(areas[dst.area]["inbound_edges"]) + 1
        if src.namespace == dst.namespace:
            namespaces[src.namespace]["internal_edges"] = int(namespaces[src.namespace]["internal_edges"]) + 1
        else:
            namespaces[src.namespace]["outbound_edges"] = int(namespaces[src.namespace]["outbound_edges"]) + 1
            namespaces[dst.namespace]["inbound_edges"] = int(namespaces[dst.namespace]["inbound_edges"]) + 1

    for bucket in (areas, namespaces):
        for entry in bucket.values():
            native_set: set[str] = entry["native_imports"]
            data_set: set[str] = entry["data_refs"]
            entry["native_imports"] = sorted(native_set)
            entry["data_refs_count"] = len(data_set)
            del entry["data_refs"]

    data_refs: dict[str, list[str]] = {}
    for path in sorted(modules):
        for ref in sorted(modules[path].data_refs):
            data_refs.setdefault(ref, []).append(path)

    data_root_totals: dict[str, int] = {root: 0 for root in DATA_ROOTS}
    for ref in data_refs:
        root = ref.split("/")[0]
        if root in data_root_totals:
            data_root_totals[root] += 1

    # Logical documents: the real coupling surface into json/ and Settings/.
    documents: dict[str, list[str]] = {}
    persistence_owner_totals: dict[str, int] = {}
    for path in sorted(modules):
        for site in modules[path].persistence_sites:
            owner = str(site["owner"])
            persistence_owner_totals[owner] = persistence_owner_totals.get(owner, 0) + 1
            doc = site["document"]
            if isinstance(doc, str) and doc:
                key = owner + ":" + doc
                if path not in documents.setdefault(key, []):
                    documents[key].append(path)

    raw_io_by_namespace: dict[str, int] = {}
    for path in sorted(modules):
        info = modules[path]
        if info.raw_io_sites:
            raw_io_by_namespace[info.namespace] = raw_io_by_namespace.get(info.namespace, 0) + len(
                info.raw_io_sites
            )

    loose_literals: dict[str, list[str]] = {}
    for path in sorted(modules):
        for literal in sorted(modules[path].loose_data_literals):
            loose_literals.setdefault(literal, []).append(path)

    native_boundary: dict[str, list[str]] = {}
    for path in sorted(modules):
        for native in sorted(modules[path].native_imports):
            native_boundary.setdefault(native, []).append(path)

    dynamic_sites: list[dict[str, Any]] = []
    for path in sorted(modules):
        for site in modules[path].dynamic_sites:
            dynamic_sites.append({"path": path, "kind": site.kind, "line": site.line, "detail": site.detail})

    broken_imports: list[dict[str, Any]] = []
    for path in sorted(modules):
        for entry in modules[path].unresolved:
            if entry.get("reason") == "broken-inrepo-import":
                broken_imports.append(
                    {
                        "path": path,
                        "area": modules[path].area,
                        "target": entry["target"],
                        "line": entry["line"],
                        "kind": entry["kind"],
                    }
                )

    orphans_total = sorted(p for p, info in modules.items() if not info.importers)
    orphans_external = sorted(
        p
        for p, info in modules.items()
        if not any(modules[q].namespace != info.namespace for q in info.importers)
    )
    single_consumer = sorted(p for p, info in modules.items() if len(info.importers) == 1)

    fan_in_rank = sorted(module_docs.values(), key=lambda m: (-int(m["fan_in"]), str(m["path"])))
    fan_out_rank = sorted(module_docs.values(), key=lambda m: (-int(m["fan_out"]), str(m["path"])))

    return {
        "meta": {
            "generator": "docs/architecture/refactor-baseline/tools/build_graph.py",
            "repo_root": repo_root.name,
            "python": sys.version.split()[0],
            "in_scope_roots": list(IN_SCOPE_ROOTS),
            "include_root_level_scripts": INCLUDE_ROOT_LEVEL_SCRIPTS,
            "excluded_dir_names": sorted(EXCLUDED_DIR_NAMES),
            "data_roots": list(DATA_ROOTS),
            "native_modules_detected": sorted(natives),
            "counts": {
                "modules": len(modules),
                "edges": len(edges),
                "parse_errors": sum(1 for info in modules.values() if info.parse_error is not None),
                "cycles": len(cycles),
                "self_loops": len(_self_loops(adjacency)),
                "orphans_zero_fan_in": len(orphans_total),
                "orphans_zero_external_fan_in": len(orphans_external),
                "single_consumer_modules": len(single_consumer),
                "dynamic_sites": len(dynamic_sites),
                "data_refs": len(data_refs),
                "logical_documents": len(documents),
                "raw_io_sites": sum(len(info.raw_io_sites) for info in modules.values()),
                "broken_imports": len(broken_imports),
            },
            "unresolved_reason_counts": dict(sorted(unresolved_counts.items())),
            "limitations": [
                "Widgets are loaded at runtime by os.walk + importlib.util.spec_from_file_location in "
                "Py4GWCoreLib/py4gwcorelib_src/WidgetManager.py. No static edge points at a widget, so "
                "zero fan-in on a widget file is NOT evidence of unreachability.",
                "Bots and Sources scripts are frequently launched by path, not imported. Same caveat.",
                "Chat-command and message-router registration is table-driven at runtime and is not an "
                "import edge.",
                "Native C++ callbacks (PyCallback, ExecuteDraw) invoke Python without a Python import.",
                "importlib.import_module with a non-literal argument is recorded as a dynamic site, not "
                "an edge.",
                "The 'unique-leaf' and 'sibling' resolutions are heuristic (they model sys.path mutation) "
                "and are weaker evidence than 'exact'.",
                "Data references are static string literals only; IniManager/JsonFactory resolve logical "
                "names at runtime, so data coupling is a floor, not a total.",
            ],
        },
        "modules": module_docs,
        "edges": edges,
        "cycles": cycles,
        "self_loops": _self_loops(adjacency),
        "broken_imports": broken_imports,
        "orphans": {
            "zero_fan_in": orphans_total,
            "zero_external_fan_in": orphans_external,
            "single_consumer": single_consumer,
        },
        "rankings": {
            "top_fan_in": [
                {"path": m["path"], "fan_in": m["fan_in"], "namespace": m["namespace"]} for m in fan_in_rank[:60]
            ],
            "top_fan_out": [
                {"path": m["path"], "fan_out": m["fan_out"], "namespace": m["namespace"]}
                for m in fan_out_rank[:60]
            ],
        },
        "areas": dict(sorted(areas.items())),
        "namespaces": dict(sorted(namespaces.items())),
        "data_coupling": {
            "by_file": dict(sorted(data_refs.items())),
            "root_totals": data_root_totals,
            "logical_documents": dict(sorted(documents.items())),
            "persistence_owner_totals": dict(sorted(persistence_owner_totals.items())),
            "raw_io_by_namespace": dict(sorted(raw_io_by_namespace.items(), key=lambda kv: -kv[1])),
            "loose_data_literals": dict(sorted(loose_literals.items())),
        },
        "native_boundary": dict(sorted(native_boundary.items())),
        "dynamic_sites": dynamic_sites,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""

    parser = argparse.ArgumentParser(description="Build the Py4GW_Reforged dependency graph.")
    # tools/ -> refactor-baseline/ -> architecture/ -> docs/ -> repository root
    default_root = Path(__file__).resolve().parents[4]
    default_out = Path(__file__).resolve().parent.parent / "graph.json"
    parser.add_argument("--repo-root", type=Path, default=default_root, help="Repository root to walk.")
    parser.add_argument("--out", type=Path, default=default_out, help="Destination for graph.json.")
    parser.add_argument("--quiet", action="store_true", help="Suppress the stdout summary.")
    args = parser.parse_args(argv)

    repo_root: Path = Path(args.repo_root).resolve()
    out_path: Path = Path(args.out)
    graph = build_graph(repo_root)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(graph, indent=2, sort_keys=False) + "\n", encoding="utf-8")

    if not args.quiet:
        meta: dict[str, Any] = graph["meta"]
        counts: dict[str, Any] = meta["counts"]
        print("repo root : " + str(repo_root))
        print("output    : " + str(out_path))
        for key in sorted(counts):
            print("  " + key.ljust(34) + " " + str(counts[key]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
