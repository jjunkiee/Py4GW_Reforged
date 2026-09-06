"""Compare a live ``sys.modules`` capture against the static boot closure.

``boot_closure.py`` computes what the injected client *should* load: the transitive
closure over module-level import edges from the widget host. It says so itself that
this is a static claim -- widget discovery via ``spec_from_file_location``, native
callbacks and computed ``importlib`` arguments are all invisible to it.

This tool closes that gap. It reads a capture taken inside a running client and
answers the question the refactor's Phase 1 exists to answer: **is the boot closure,
and therefore the Phase 2 cut ranking, built on sand?**

The capture has three stages, and they are not interchangeable:

``import``
    Taken after the host's module-level imports and before anything else runs. This
    is the only stage the static boot closure models, so it is the only
    apples-to-apples comparison. A module here that the closure did not predict is
    the finding that matters.

``post_discover``
    After ``WidgetHandler.discover()``. Despite the name, discovery is where widget
    code executes: ``_load_widget_module()`` reads each widget's saved enabled state
    and calls ``enable()``, which loads the module. Measured live at 152 widget
    modules here against 0 at ``import``.

``post_bootstrap``
    After ``_apply_ini_configuration()``. It re-applies saved state and force-enables
    the ``Widgets/System`` tier, but the modules are already loaded by then, so it
    adds nothing to ``sys.modules``. The growth from ``import`` to here is the
    startup cost the MVP gate proposes to stop paying, and it is measured rather
    than estimated.

Only modules resolving inside the repository are compared. Standard library,
site-packages and native extension modules are counted and then set aside: they are
real cost but not this refactor's subject.

Each stage records ``name -> __file__`` at the moment it is taken, not names now and
paths later. That ordering is load-bearing: several widgets call
``Utils.ClearSubModules`` during discovery, which deletes ``sys.modules`` entries, so
a path map built at write time silently loses modules that were present earlier.
Captures from the superseded schema are rejected rather than reinterpreted.

This tool consumes the other two generators' published output rather than importing
them, so like them it imports nothing and runs outside the injected client::

    python docs/architecture/refactor-baseline/tools/build_graph.py
    python docs/architecture/refactor-baseline/tools/boot_closure.py --out boot-closure.json
    python docs/architecture/refactor-baseline/tools/boot_capture_compare.py \\
        --closure boot-closure.json --capture boot-capture.json

Deterministic: every collection is sorted before output, so a rerun on unchanged
inputs is byte-identical.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from typing import cast

#: Stage order is meaningful: each is a superset of the one before it.
STAGES: tuple[str, ...] = ("import", "post_discover", "post_bootstrap")


def load_json(path: Path, produced_by: str) -> dict[str, Any]:
    """Read a JSON object, failing with a message a human can act on."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit("cannot read %s (%s): %s" % (path, produced_by, exc)) from exc
    try:
        data: Any = json.loads(text)
    except ValueError as exc:
        raise SystemExit("%s is not valid JSON: %s" % (path, exc)) from exc
    if not isinstance(data, dict):
        raise SystemExit("%s does not contain a JSON object" % path)
    return cast("dict[str, Any]", data)


def module_index(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The graph's module table, validated enough to fail early and clearly."""

    raw: Any = graph.get("modules")
    if not isinstance(raw, dict):
        raise SystemExit("graph has no 'modules' object; rerun build_graph.py")
    modules = cast("dict[str, Any]", raw)
    typed: dict[str, dict[str, Any]] = {}
    for key, info in modules.items():
        if isinstance(info, dict):
            typed[str(key)] = cast("dict[str, Any]", info)
    return typed


def predicted_modules(closure: dict[str, Any]) -> list[str]:
    """The boot closure as reported by ``boot_closure.py --out``."""

    reported: Any = closure.get("boot_closure_modules")
    if not isinstance(reported, list):
        raise SystemExit(
            "closure report has no 'boot_closure_modules' list; produce it with:\n"
            "  python docs/architecture/refactor-baseline/tools/boot_closure.py --out boot-closure.json"
        )
    return [str(entry) for entry in cast("list[Any]", reported)]


def repo_key(file_path: str, repo_root: Path, index: dict[str, str]) -> str | None:
    """Map a live ``__file__`` to its ``graph.json`` module key, or None if outside.

    Windows supplies inconsistent drive casing, so the lookup is case-folded. The
    graph's own spelling is returned, so downstream output stays consistent with
    every other report in this directory.
    """

    try:
        resolved = Path(file_path).resolve()
    except OSError:
        return None
    try:
        relative = resolved.relative_to(repo_root)
    except ValueError:
        return None
    return index.get(relative.as_posix().casefold())


def stage_modules(
    capture: dict[str, Any],
    repo_root: Path,
    index: dict[str, str],
) -> tuple[dict[str, set[str]], dict[str, int]]:
    """Per stage: the repository modules loaded, and how many entries were external."""

    if "module_files" in capture:
        raise SystemExit(
            "this capture is from the superseded schema 1 probe and cannot be trusted.\n"
            "It stored module paths once at write time rather than per stage, so any module\n"
            "removed from sys.modules in between - widgets call Utils.ClearSubModules during\n"
            "discovery - lost its path and was miscounted as external. Retake the capture with\n"
            "the current probe; the procedure is in\n"
            "docs/architecture/records/refactor-phase-1-boot-proof.md"
        )

    raw_stages: Any = capture.get("stages")
    if not isinstance(raw_stages, dict):
        raise SystemExit("capture is missing its 'stages' object")
    all_stages = cast("dict[str, Any]", raw_stages)

    in_repo: dict[str, set[str]] = {}
    external: dict[str, int] = {}
    for stage in STAGES:
        entries: Any = all_stages.get(stage)
        if not isinstance(entries, dict):
            continue
        keys: set[str] = set()
        outside = 0
        for _name, path in cast("dict[str, Any]", entries).items():
            key = repo_key(path, repo_root, index) if isinstance(path, str) else None
            if key is None:
                outside += 1
            else:
                keys.add(key)
        in_repo[stage] = keys
        external[stage] = outside
    return in_repo, external


def module_loc(modules: dict[str, dict[str, Any]], key: str) -> int:
    """Line count for *key*, or zero when the graph does not know it."""

    info = modules.get(key)
    if info is None:
        return 0
    try:
        return int(info["loc"])
    except (KeyError, TypeError, ValueError):
        return 0


def summarise(modules: dict[str, dict[str, Any]], keys: set[str]) -> dict[str, Any]:
    """Module count, line count and per-area breakdown for a set of module keys."""

    areas: dict[str, int] = {}
    total = 0
    for key in keys:
        total += module_loc(modules, key)
        info = modules.get(key)
        if info is None:
            continue
        area = str(info.get("area", "unknown"))
        areas[area] = areas.get(area, 0) + 1
    return {
        "modules": len(keys),
        "loc": total,
        "by_area": dict(sorted(areas.items(), key=lambda kv: (-kv[1], kv[0]))),
    }


def module_rows(modules: dict[str, dict[str, Any]], keys: list[str]) -> list[dict[str, Any]]:
    """Sorted module/loc rows for report output."""

    return [{"module": key, "loc": module_loc(modules, key)} for key in keys]


def build_report(
    modules: dict[str, dict[str, Any]],
    predicted: set[str],
    live: dict[str, set[str]],
    external: dict[str, int],
    capture_meta: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the deterministic comparison body."""

    at_import = live.get("import", set())
    unpredicted = sorted(at_import - predicted)
    unloaded = sorted(predicted - at_import)

    stages: dict[str, Any] = {}
    previous: set[str] | None = None
    for stage in STAGES:
        if stage not in live:
            continue
        keys = live[stage]
        entry = summarise(modules, keys)
        entry["external_modules"] = external.get(stage, 0)
        if previous is not None:
            entry["added_since_previous"] = summarise(modules, keys - previous)
        stages[stage] = entry
        previous = keys

    bootstrap_cost = summarise(modules, live.get("post_bootstrap", set()) - at_import)

    if unpredicted:
        verdict = (
            "RANKING AT RISK: %d repository module(s) load at the import stage that the "
            "static closure does not predict. Re-derive the Phase 2 cut ranking before "
            "spending effort on it." % len(unpredicted)
        )
    elif not at_import:
        verdict = "NO DATA: the capture contains no in-repository modules at the import stage."
    else:
        verdict = (
            "RANKING STANDS: every module loaded at the import stage was predicted. The "
            "static boot closure is a ceiling for that stage, so the cut ranking rests on "
            "observed behaviour."
        )

    return {
        "meta": {
            "generator": "docs/architecture/refactor-baseline/tools/boot_capture_compare.py",
            "inputs": [
                "docs/architecture/refactor-baseline/graph.json",
                "boot_closure.py --out",
                "host probe capture",
            ],
            "capture": capture_meta,
            "limitations": [
                "Only the import stage is comparable to the static boot closure; the "
                "later stages measure dynamic widget loading the closure cannot model.",
                "Modules resolving outside the repository are counted, not compared.",
                "A capture reflects one account's saved widget configuration.",
            ],
        },
        "verdict": verdict,
        "predicted_boot_closure": summarise(modules, predicted),
        "stages": stages,
        "loaded_but_not_predicted": module_rows(modules, unpredicted),
        "predicted_but_not_loaded": module_rows(modules, unloaded),
        "widget_bootstrap_cost": bootstrap_cost,
    }


def print_rows(title: str, rows: list[dict[str, Any]], limit: int = 40) -> None:
    """Print a capped module/loc listing."""

    if not rows:
        return
    print()
    print(title)
    for row in rows[:limit]:
        print("  %8d loc  %s" % (row["loc"], row["module"]))
    if len(rows) > limit:
        print("  ... %d more; see the JSON output" % (len(rows) - limit))


def print_summary(report: dict[str, Any]) -> None:
    """Human-readable digest. The JSON output carries the full detail."""

    predicted: dict[str, Any] = report["predicted_boot_closure"]
    print(
        "  predicted boot closure   %6d modules / %8d loc"
        % (predicted["modules"], predicted["loc"])
    )
    stages: dict[str, Any] = report["stages"]
    for stage in STAGES:
        entry: Any = stages.get(stage)
        if not isinstance(entry, dict):
            continue
        print(
            "  live: %-15s    %6d modules / %8d loc   (+%d external)"
            % (stage, entry["modules"], entry["loc"], entry["external_modules"])
        )

    unpredicted: list[dict[str, Any]] = report["loaded_but_not_predicted"]
    unloaded: list[dict[str, Any]] = report["predicted_but_not_loaded"]
    cost: dict[str, Any] = report["widget_bootstrap_cost"]
    print()
    print("  loaded but not predicted %6d" % len(unpredicted))
    print("  predicted but not loaded %6d" % len(unloaded))
    print("  widget bootstrap cost    %6d modules / %8d loc" % (cost["modules"], cost["loc"]))

    print_rows("loaded but not predicted (the finding that matters):", unpredicted)
    print_rows("predicted but not loaded (static over-prediction):", unloaded)

    print()
    print(report["verdict"])


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns non-zero when the capture contradicts the static closure."""

    repo_root = Path(__file__).resolve().parents[4]
    baseline = repo_root / "docs" / "architecture" / "refactor-baseline"
    parser = argparse.ArgumentParser(
        description="Compare a live sys.modules capture against the static boot closure."
    )
    parser.add_argument(
        "--capture",
        type=Path,
        default=repo_root / "boot-capture.json",
        help="Capture written by the host probe (default: boot-capture.json at the repo root).",
    )
    parser.add_argument(
        "--closure",
        type=Path,
        default=repo_root / "boot-closure.json",
        help="Report from boot_closure.py --out (default: boot-closure.json at the repo root).",
    )
    parser.add_argument(
        "--graph",
        type=Path,
        default=baseline / "graph.json",
        help="Import graph produced by build_graph.py.",
    )
    parser.add_argument("--out", type=Path, default=None, help="Optional JSON destination.")
    args = parser.parse_args(argv)

    graph_path = Path(args.graph)
    closure_path = Path(args.closure)
    capture_path = Path(args.capture)

    modules = module_index(load_json(graph_path, "build_graph.py"))
    predicted = set(predicted_modules(load_json(closure_path, "boot_closure.py --out")))
    capture = load_json(capture_path, "the host probe")

    index = {key.casefold(): key for key in modules}
    live, external = stage_modules(capture, repo_root, index)
    if not live:
        raise SystemExit("capture contains none of the expected stages: %s" % ", ".join(STAGES))

    raw_meta: Any = capture.get("meta")
    capture_meta: dict[str, Any] = (
        cast("dict[str, Any]", raw_meta) if isinstance(raw_meta, dict) else {}
    )
    report = build_report(modules, predicted, live, external, capture_meta)

    print("repo root  : %s" % repo_root)
    print("graph      : %s" % graph_path)
    print("closure    : %s" % closure_path)
    print("capture    : %s" % capture_path)
    print()
    print_summary(report)

    if args.out is not None:
        out_path = Path(args.out)
        out_path.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        print()
        print("wrote %s" % out_path)

    return 1 if report["loaded_but_not_predicted"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
