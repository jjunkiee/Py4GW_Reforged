"""Boot-closure analyser for the Py4GW_Reforged refactor sequencing plan.

``build_graph.py`` answers *what imports what*. This tool answers a narrower question the
refactor needs and the graph alone cannot express: **what does the injected client actually
load when the C++ DLL runs the widget host?**

The distinction matters because ``graph.json`` records every import statement, including the
ones nested inside function bodies. A function-level ``import`` is a real dependency but it
does not execute at module load, so it does not contribute to the cost of booting. Counting
it as if it did overstates the startup surface by roughly 40% in this repository.

This tool therefore re-parses each in-scope source file and classifies every edge in
``graph.json`` as:

``module-level``
    The import statement executes during module load. Reached by descending from the module
    body through ``if`` / ``try`` / ``with`` / ``for`` / ``while`` / ``class`` bodies, all of
    which run at import time.

``deferred``
    The import statement sits inside a ``def`` or ``async def`` body and runs only when that
    function is called.

It then computes the **boot closure** -- the transitive closure over module-level edges only,
from the host entry point -- and ranks single-edge cuts by how much of that closure they
release. That ranking is the input to the refactor's demolition order.

What this tool can and cannot prove
-----------------------------------

It can prove which modules are *statically reachable* at load time. It cannot prove what a
live client loads. Native callbacks, ``spec_from_file_location`` widget discovery, and
``importlib`` with a computed argument are all invisible here, exactly as they are to
``build_graph.py``. The boot closure is therefore a **floor for the import-driven cost and a
static claim, not a runtime observation.** Confirm it against ``sys.modules`` on an injected
client before treating the cut ranking as settled.

Usage::

    python docs/architecture/refactor-baseline/tools/boot_closure.py
    python docs/architecture/refactor-baseline/tools/boot_closure.py --top 25
    python docs/architecture/refactor-baseline/tools/boot_closure.py --out boot-closure.json
    python docs/architecture/refactor-baseline/tools/boot_closure.py --root "Py4GW_widget_manager.py"
"""

from __future__ import annotations

import argparse
import ast
import json
from collections import Counter
from collections import deque
from pathlib import Path
from typing import Any

#: Entry point the C++ DLL runs every draw frame. The default boot root.
DEFAULT_ROOTS: tuple[str, ...] = ("Py4GW_widget_manager.py",)

#: Areas the refactor's minimal baseline retains. Everything else is the consumer tier,
#: which the sequencing plan removes wholesale once the edges below are severed.
DEFAULT_KEEP_AREAS: tuple[str, ...] = (
    "Py4GWCoreLib",
    "Py4GW_Reforged_Launcher",
    "Root",
    "py4gw_bridge",
)


def module_level_import_lines(source_path: Path) -> set[int] | None:
    """Line numbers of import statements that execute when *source_path* is loaded.

    Descends through blocks that run at import time and refuses to descend into function
    bodies, which do not. Returns ``None`` when the file cannot be read or parsed; callers
    treat that as "assume every edge is module-level", which is the conservative direction.
    """

    try:
        source = source_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    lines: set[int] = set()

    def walk(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                lines.add(node.lineno)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue  # a def body runs on call, not on import
            elif isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While)):
                walk(node.body)
                walk(node.orelse)
            elif isinstance(node, (ast.Try, ast.TryStar)):
                walk(node.body)
                for handler in node.handlers:
                    walk(handler.body)
                walk(node.orelse)
                walk(node.finalbody)
            elif isinstance(node, (ast.With, ast.AsyncWith, ast.ClassDef)):
                walk(node.body)

    walk(tree.body)
    return lines


class ImportGraph:
    """Module-level and full adjacency built from ``graph.json`` plus a source re-parse."""

    def __init__(self, graph: dict[str, Any], repo_root: Path) -> None:
        self.modules: dict[str, dict[str, Any]] = graph["modules"]
        self.module_level: dict[str, set[str]] = {}
        self.static: dict[str, set[str]] = {}
        self.edge_lines: dict[tuple[str, str], int] = {}
        self.deferred_edges: int = 0
        self._init_cache: dict[str, tuple[str, ...]] = {}

        cache: dict[str, set[int] | None] = {}
        edges: list[dict[str, Any]] = graph["edges"]
        for edge in edges:
            source: str = edge["source"]
            target: str = edge["target"]
            line: int = edge["line"]
            self.static.setdefault(source, set()).add(target)
            self.edge_lines.setdefault((source, target), line)
            if source not in cache:
                cache[source] = module_level_import_lines(repo_root / source)
            allowed = cache[source]
            if allowed is None or line in allowed:
                self.module_level.setdefault(source, set()).add(target)
            else:
                self.deferred_edges += 1

    def package_inits(self, module: str) -> tuple[str, ...]:
        """The package ``__init__.py`` files Python loads in order to load *module*.

        Importing ``a.b.c`` executes ``a/__init__.py`` and ``a/b/__init__.py`` before
        ``a/b/c.py``. There is no import statement for that, so ``build_graph.py``
        records no edge, and a closure built from edges alone misses both the
        ``__init__`` files and everything they import in turn.

        Confirmed against a live client: every module this rule adds was observed in
        ``sys.modules`` and had been absent from the closure. See
        ``../records/refactor-phase-1-boot-proof.md``.
        """

        cached = self._init_cache.get(module)
        if cached is not None:
            return cached
        parts = module.split("/")[:-1]
        found: list[str] = []
        for depth in range(len(parts)):
            candidate = "/".join(parts[: depth + 1]) + "/__init__.py"
            if candidate != module and candidate in self.modules:
                found.append(candidate)
        result = tuple(found)
        self._init_cache[module] = result
        return result

    def closure(
        self,
        roots: tuple[str, ...],
        adjacency: dict[str, set[str]],
        skip: tuple[str, str] | None = None,
    ) -> set[str]:
        """Transitive closure from *roots*, optionally severing one edge.

        Follows recorded import edges and, for every module reached, the package
        ``__init__`` files Python must execute to get to it. A cut that releases a
        module therefore also releases its package ``__init__`` files, unless
        something else in the closure still reaches into the same package.
        """

        seen: set[str] = set()
        stack: list[str] = list(roots)
        while stack:
            module = stack.pop()
            if module in seen:
                continue
            seen.add(module)
            for parent in self.package_inits(module):
                if parent not in seen:
                    stack.append(parent)
            for target in adjacency.get(module, ()):
                if skip is not None and (module, target) == skip:
                    continue
                if target not in seen:
                    stack.append(target)
        return seen

    def loc(self, modules: set[str]) -> int:
        """Total line count of *modules* that the graph knows about."""

        return sum(int(self.modules[m]["loc"]) for m in modules if m in self.modules)

    def shortest_path(self, roots: tuple[str, ...], target: str) -> list[str] | None:
        """Shortest module-level import chain from *roots* to *target*, for citation."""

        previous: dict[str, str] = {}
        queue: deque[str] = deque(roots)
        seen: set[str] = set(roots)
        while queue:
            module = queue.popleft()
            if module == target:
                chain = [module]
                while chain[-1] in previous:
                    chain.append(previous[chain[-1]])
                return list(reversed(chain))
            for nxt in sorted(self.module_level.get(module, ())):
                if nxt not in seen:
                    seen.add(nxt)
                    previous[nxt] = module
                    queue.append(nxt)
        return None


def rank_source_cuts(
    graph: ImportGraph, roots: tuple[str, ...], boot: set[str]
) -> list[dict[str, Any]]:
    """Rank modules by how much emptying *all* their module-level imports releases.

    Single-edge ranking is blind to a module whose cost is spread thinly across many
    edges. ``Py4GWCoreLib/__init__.py`` has 41 module-level imports; no one of them
    releases much, while emptying it releases roughly half the boot closure. That is
    what a facade looks like from the boot path, and ranking edges alone hides it
    behind cuts worth a tenth as much.

    This is not a proposal to delete those imports. It measures which owner is
    carrying the startup cost, which is the question the demolition order needs
    answered first.
    """

    ranked: list[dict[str, Any]] = []
    for source in sorted(boot):
        if source in roots:
            continue  # emptying the entry point is deleting the program, not a cut
        targets = graph.module_level.get(source)
        if not targets:
            continue
        adjacency = dict(graph.module_level)
        adjacency[source] = set()
        released = boot - graph.closure(roots, adjacency)
        if not released:
            continue
        ranked.append(
            {
                "source": source,
                "module_level_imports": len(targets),
                "modules_released": len(released),
                "loc_released": graph.loc(released),
            }
        )
    ranked.sort(
        key=lambda c: (-int(c["modules_released"]), -int(c["loc_released"]), str(c["source"]))
    )
    return ranked


def rank_cuts(graph: ImportGraph, roots: tuple[str, ...], boot: set[str]) -> list[dict[str, Any]]:
    """Rank single module-level edges by how much of the boot closure severing them releases."""

    ranked: list[dict[str, Any]] = []
    for source in sorted(boot):
        for target in sorted(graph.module_level.get(source, set())):
            if target not in boot:
                continue
            remaining = graph.closure(roots, graph.module_level, skip=(source, target))
            released = boot - remaining
            if not released:
                continue
            ranked.append(
                {
                    "source": source,
                    "line": graph.edge_lines.get((source, target), 0),
                    "target": target,
                    "modules_released": len(released),
                    "loc_released": graph.loc(released),
                }
            )
    ranked.sort(
        key=lambda c: (
            -int(c["modules_released"]),
            -int(c["loc_released"]),
            str(c["source"]),
            str(c["target"]),
        )
    )
    return ranked


def severance_edges(graph: ImportGraph, keep_areas: tuple[str, ...]) -> list[dict[str, Any]]:
    """Every import from a retained area into the consumer tier.

    These are the edges that must be cut before the consumer tier can be deleted. The
    ``module-level`` ones break the core on import and must go first; the ``deferred`` ones
    break it only when the owning function runs, which is harder to notice and no less real.
    """

    keep = frozenset(keep_areas)
    found: list[dict[str, Any]] = []
    for source in sorted(graph.static):
        targets = graph.static[source]
        source_area = str(graph.modules.get(source, {}).get("area", ""))
        if source_area not in keep:
            continue
        for target in sorted(targets):
            target_area = str(graph.modules.get(target, {}).get("area", ""))
            if target_area in keep:
                continue
            is_module_level = target in graph.module_level.get(source, set())
            found.append(
                {
                    "source": source,
                    "line": graph.edge_lines.get((source, target), 0),
                    "target": target,
                    "scope": "module-level" if is_module_level else "deferred",
                }
            )
    found.sort(key=lambda e: (str(e["scope"]), str(e["source"]), int(e["line"]), str(e["target"])))
    return found


def build_report(
    graph: ImportGraph,
    roots: tuple[str, ...],
    top: int,
    keep_areas: tuple[str, ...],
) -> dict[str, Any]:
    """Assemble the deterministic report body."""

    boot = graph.closure(roots, graph.module_level)
    static = graph.closure(roots, graph.static)
    by_namespace = Counter(str(graph.modules[m]["namespace"]) for m in boot if m in graph.modules)

    keep = frozenset(keep_areas)
    area_modules: Counter[str] = Counter()
    area_loc: Counter[str] = Counter()
    for info in graph.modules.values():
        area = str(info["area"])
        area_modules[area] += 1
        area_loc[area] += int(info["loc"])
    consumer_areas = sorted(a for a in area_modules if a not in keep)
    severance = severance_edges(graph, keep_areas)

    return {
        "meta": {
            "generator": "docs/architecture/refactor-baseline/tools/boot_closure.py",
            "source_graph": "docs/architecture/refactor-baseline/graph.json",
            "roots": list(roots),
            "keep_areas": list(keep_areas),
            "limitations": [
                "Static reachability at load time only; this is not a runtime observation.",
                "Widget discovery (spec_from_file_location), native callbacks and computed "
                "importlib arguments are invisible here, as they are to build_graph.py.",
                "A file that cannot be parsed has all of its edges treated as module-level.",
            ],
        },
        "totals": {
            "modules_in_graph": len(graph.modules),
            "deferred_edges": graph.deferred_edges,
            "static_closure_modules": len(static),
            "static_closure_loc": graph.loc(static),
            "boot_closure_modules": len(boot),
            "boot_closure_loc": graph.loc(boot),
            "consumer_tier_modules": sum(area_modules[a] for a in consumer_areas),
            "consumer_tier_loc": sum(area_loc[a] for a in consumer_areas),
            "severance_edges_module_level": sum(1 for e in severance if e["scope"] == "module-level"),
            "severance_edges_deferred": sum(1 for e in severance if e["scope"] == "deferred"),
        },
        "areas": {
            area: {
                "modules": area_modules[area],
                "loc": area_loc[area],
                "disposition": "keep" if area in keep else "consumer-tier",
            }
            for area in sorted(area_modules, key=lambda a: (-area_loc[a], a))
        },
        "boot_closure_by_namespace": dict(sorted(by_namespace.items(), key=lambda kv: (-kv[1], kv[0]))),
        "boot_closure_modules": sorted(boot),
        "severance_edges": severance,
        "top_source_cuts": rank_source_cuts(graph, roots, boot)[:top],
        "top_cuts": rank_cuts(graph, roots, boot)[:top],
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""

    parser = argparse.ArgumentParser(description="Analyse the Py4GW_Reforged boot closure.")
    # tools/ -> refactor-baseline/ -> architecture/ -> docs/ -> repository root
    default_root = Path(__file__).resolve().parents[4]
    default_graph = Path(__file__).resolve().parent.parent / "graph.json"
    parser.add_argument("--repo-root", type=Path, default=default_root, help="Repository root.")
    parser.add_argument("--graph", type=Path, default=default_graph, help="graph.json to read.")
    parser.add_argument("--root", action="append", default=None, help="Boot entry point; repeatable.")
    parser.add_argument("--keep-area", action="append", default=None, help="Retained area; repeatable.")
    parser.add_argument("--top", type=int, default=15, help="How many ranked cuts to report.")
    parser.add_argument("--out", type=Path, default=None, help="Optional JSON destination.")
    parser.add_argument("--quiet", action="store_true", help="Suppress the stdout summary.")
    args = parser.parse_args(argv)

    repo_root: Path = Path(args.repo_root).resolve()
    graph_path: Path = Path(args.graph)
    roots: tuple[str, ...] = tuple(args.root) if args.root else DEFAULT_ROOTS
    keep_areas: tuple[str, ...] = tuple(args.keep_area) if args.keep_area else DEFAULT_KEEP_AREAS

    graph = ImportGraph(json.loads(graph_path.read_text(encoding="utf-8")), repo_root)
    report = build_report(graph, roots, int(args.top), keep_areas)

    out_path: Path | None = args.out
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8")

    if not args.quiet:
        totals: dict[str, Any] = report["totals"]
        print("repo root  : " + str(repo_root))
        print("graph      : " + str(graph_path))
        print("boot roots : " + ", ".join(roots))
        print("")
        for key in (
            "modules_in_graph",
            "deferred_edges",
            "static_closure_modules",
            "static_closure_loc",
            "boot_closure_modules",
            "boot_closure_loc",
            "consumer_tier_modules",
            "consumer_tier_loc",
            "severance_edges_module_level",
            "severance_edges_deferred",
        ):
            print("  " + key.ljust(30) + " " + str(totals[key]))
        print("")
        print("severance edges (retained area -> consumer tier; cut before deleting):")
        severance: list[dict[str, Any]] = report["severance_edges"]
        for edge in severance:
            print("  [{0}] {1}:{2}".format(edge["scope"], edge["source"], edge["line"]))
            print("           -> " + str(edge["target"]))
        print("")
        print("top cuts (modules / loc released from the boot closure):")
        cuts: list[dict[str, Any]] = report["top_cuts"]
        for cut in cuts:
            print(
                "  -{0:>4} mods  -{1:>7} loc   {2}:{3}".format(
                    cut["modules_released"], cut["loc_released"], cut["source"], cut["line"]
                )
            )
            print("                              -> " + str(cut["target"]))
        print("")
        print("top owners (releasing all of a module's module-level imports):")
        owners: list[dict[str, Any]] = report["top_source_cuts"]
        for owner in owners:
            print(
                "  -{0:>4} mods  -{1:>7} loc   {2} ({3} imports)".format(
                    owner["modules_released"],
                    owner["loc_released"],
                    owner["source"],
                    owner["module_level_imports"],
                )
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
