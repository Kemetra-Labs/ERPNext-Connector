"""RT-1460 (audit A-21): fail on a NEW import cycle in the Connector package.

    python integrations/erpnext-connector/scripts/check_import_cycles.py            # check
    python integrations/erpnext-connector/scripts/check_import_cycles.py --update   # rewrite the baseline

Parses every module of ``retail_tower_erpnext_connector`` (not its tests) with ``ast`` and builds the
module import graph from EVERY import statement, including the ones inside a function, a method or
an ``if TYPE_CHECKING`` block. A function-local import is the usual way to hide a cycle, so it
counts as an edge. A cycle is a strongly connected component of two or more modules.

The baseline (``scripts/import-cycles.baseline.json``) lists the cycles that are accepted today and
is shrink-only: a cycle that is not in it fails, and a baseline entry that no longer exists also
fails (run ``--update`` to drop it). ``--update`` refuses to add a cycle. The baseline is ``[]``: the
Connector has no cycle. The implicit import of a parent package is not an edge (a package
``__init__`` that re-exports a submodule is not a cycle). Stdlib only, Python 3.14. No frappe import.

Exit codes: 0 pass, 1 a new or stale cycle, 2 unreadable input.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

PRODUCT = Path(__file__).resolve().parents[1]
PACKAGE = "retail_tower_erpnext_connector"
BASELINE = Path(__file__).with_name("import-cycles.baseline.json")
SKIP_PARTS = {"tests", "__pycache__"}


def module_name(path: Path, root: Path) -> str:
	parts = list(path.relative_to(root).with_suffix("").parts)
	if parts[-1] == "__init__":
		parts.pop()
	return ".".join(parts)


def discover(root: Path) -> dict[str, Path]:
	modules: dict[str, Path] = {}
	for path in sorted((root / PACKAGE).rglob("*.py")):
		if SKIP_PARTS & set(path.relative_to(root).parts):
			continue
		modules[module_name(path, root)] = path
	return modules


def _resolve(importer: str, is_package: bool, node: ast.ImportFrom, known: set[str]) -> set[str]:
	"""The in-package modules a ``from X import a, b`` statement depends on."""
	if node.level:
		base_parts = importer.split(".") if is_package else importer.split(".")[:-1]
		if node.level - 1 > len(base_parts):
			return set()
		base_parts = base_parts[: len(base_parts) - (node.level - 1)]
		base = ".".join([*base_parts, *(node.module.split(".") if node.module else [])])
	else:
		base = node.module or ""
	if base != PACKAGE and not base.startswith(PACKAGE + "."):
		return set()
	targets: set[str] = set()
	for alias in node.names:
		sub = f"{base}.{alias.name}"
		if sub in known:  # `from . import module`
			targets.add(sub)
		elif base in known:  # `from .module import name`
			targets.add(base)
	return targets


def build_graph(modules: dict[str, Path]) -> dict[str, set[str]]:
	known = set(modules)
	graph: dict[str, set[str]] = {name: set() for name in modules}
	for name, path in modules.items():
		try:
			tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
		except (OSError, SyntaxError, UnicodeDecodeError) as exc:
			raise SystemExit(f"import-cycles: cannot read {path}: {exc}") from exc
		is_package = path.name == "__init__.py"
		for node in ast.walk(tree):
			if isinstance(node, ast.Import):
				for alias in node.names:
					# `import a.b.c` binds the deepest in-package module it names
					parts = alias.name.split(".")
					for i in range(len(parts), 0, -1):
						if ".".join(parts[:i]) in known:
							graph[name].add(".".join(parts[:i]))
							break
			elif isinstance(node, ast.ImportFrom):
				graph[name] |= _resolve(name, is_package, node, known)
	return graph


def cycles(graph: dict[str, set[str]]) -> list[list[str]]:
	"""Strongly connected components with two or more modules, or a self-import (Tarjan)."""
	index: dict[str, int] = {}
	low: dict[str, int] = {}
	stack: list[str] = []
	on_stack: set[str] = set()
	out: list[list[str]] = []

	def visit(node: str) -> None:
		index[node] = low[node] = len(index)
		stack.append(node)
		on_stack.add(node)
		for nxt in sorted(graph[node]):
			if nxt not in index:
				visit(nxt)
				low[node] = min(low[node], low[nxt])
			elif nxt in on_stack:
				low[node] = min(low[node], index[nxt])
		if low[node] != index[node]:
			return
		component = []
		while not component or component[-1] != node:
			component.append(stack.pop())
			on_stack.discard(component[-1])
		if len(component) > 1 or node in graph[node]:
			out.append(sorted(component))

	for root in sorted(graph):  # recursion depth is bounded by the module count (about 100)
		if root not in index:
			visit(root)
	return sorted(out)


def load_baseline(path: Path = BASELINE) -> list[list[str]]:
	try:
		data = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		raise SystemExit(f"import-cycles: cannot read baseline {path}: {exc}") from exc
	if not isinstance(data, list) or any(
		not isinstance(c, list) or not all(isinstance(m, str) for m in c) for c in data
	):
		raise SystemExit(f"import-cycles: baseline {path} must be a list of module lists")
	return sorted(sorted(c) for c in data)


def compare(found: list[list[str]], baseline: list[list[str]]) -> tuple[list[list[str]], list[list[str]]]:
	return [c for c in found if c not in baseline], [c for c in baseline if c not in found]


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(
		description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
	)
	parser.add_argument(
		"--update", action="store_true", help="drop fixed cycles from the baseline (never adds one)"
	)
	args = parser.parse_args(argv)
	found = cycles(build_graph(discover(PRODUCT)))
	baseline = load_baseline()
	new, stale = compare(found, baseline)
	if args.update:
		if new:
			print("import-cycles: refusing to add a cycle to the baseline:", file=sys.stderr)
		else:
			BASELINE.write_text(json.dumps(found, indent="\t") + "\n", encoding="utf-8")
			print(f"import-cycles: baseline now has {len(found)} cycle(s)")
			return 0
	for cycle in new:
		print(f"import-cycles: NEW cycle: {' <-> '.join(cycle)}", file=sys.stderr)
	for cycle in stale:
		print(f"import-cycles: baseline cycle is gone, run --update: {' <-> '.join(cycle)}", file=sys.stderr)
	if new or stale:
		return 1
	print(f"import-cycles: ok ({len(found)} cycle(s), baseline {len(baseline)})")
	return 0


if __name__ == "__main__":
	sys.exit(main())
