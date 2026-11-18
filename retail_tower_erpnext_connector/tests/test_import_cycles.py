"""RT-1460 (audit A-21): the Connector import-cycle check, and the guard that the package has none.

Frappe-free. ``scripts/check_import_cycles.py`` parses every module with ``ast``, counts function-local
imports as edges, and compares the cycles it finds with a shrink-only baseline. Running it from here
wires it into the Connector unit-test job (and the bench discovery, which imports every test module).
"""

import importlib.util
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_import_cycles.py"
_spec = importlib.util.spec_from_file_location("check_import_cycles", SCRIPT)
cic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cic)

PKG = cic.PACKAGE


def make_package(root: Path, files: dict[str, str]) -> dict[str, Path]:
	for rel, body in files.items():
		path = root / PKG / rel
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(textwrap.dedent(body), encoding="utf-8")
	for directory in {p.parent for p in (root / PKG).rglob("*.py")} | {root / PKG}:
		(directory / "__init__.py").touch()
	return cic.discover(root)


def found(tmp_path: Path, files: dict[str, str]) -> list[list[str]]:
	return cic.cycles(cic.build_graph(make_package(tmp_path, files)))


def test_the_connector_package_has_no_import_cycle():
	"""The real package, with function-local imports counted: no cycle, and the baseline is empty."""
	graph = cic.build_graph(cic.discover(cic.PRODUCT))
	new, stale = cic.compare(cic.cycles(graph), cic.load_baseline())
	assert new == [], f"new import cycle(s): {new}"
	assert stale == [], f"baseline entries that no longer exist (run --update): {stale}"
	assert cic.load_baseline() == []


def test_a_top_level_cycle_is_found(tmp_path):
	cycles = found(tmp_path, {"a.py": "from .b import y\nx = 1\n", "b.py": "from .a import x\ny = 1\n"})
	assert cycles == [[f"{PKG}.a", f"{PKG}.b"]]


def test_a_function_local_import_still_counts_as_a_cycle(tmp_path):
	"""The way the Connector hid builder <-> uom: the second edge is inside a function."""
	cycles = found(
		tmp_path,
		{
			"a.py": "from .b import y\nx = 1\n",
			"b.py": "def f():\n\tfrom .a import x\n\treturn x\n\ny = 1\n",
		},
	)
	assert cycles == [[f"{PKG}.a", f"{PKG}.b"]]


def test_a_type_checking_import_counts(tmp_path):
	cycles = found(
		tmp_path,
		{
			"a.py": "from .b import y\nx = 1\n",
			"b.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n\tfrom .a import x\ny = 1\n",
		},
	)
	assert len(cycles) == 1


def test_a_three_module_cycle_and_its_members(tmp_path):
	cycles = found(
		tmp_path,
		{
			"a.py": "from . import b\n",
			"b.py": "from . import c\n",
			"c.py": "from . import a\n",
			"d.py": "from . import a\n",
		},
	)
	assert cycles == [[f"{PKG}.a", f"{PKG}.b", f"{PKG}.c"]]


def test_an_acyclic_graph_with_a_shared_leaf_has_none(tmp_path):
	cycles = found(
		tmp_path,
		{
			"errors.py": "class E(Exception):\n\tpass\n",
			"uom.py": "from .errors import E\n",
			"builder.py": "from .errors import E\nfrom .uom import f\n",
		},
	)
	assert cycles == []


def test_a_parent_package_init_that_reexports_is_not_a_cycle(tmp_path):
	cycles = found(
		tmp_path,
		{
			"sub/__init__.py": "from .leaf import value\n",
			"sub/leaf.py": "value = 1\n",
			"other.py": "from .sub import value\n",
		},
	)
	assert cycles == []


def test_relative_import_of_a_sibling_package_resolves(tmp_path):
	cycles = found(
		tmp_path,
		{
			"p/a.py": "from ..q.b import y\nx = 1\n",
			"q/b.py": "def f():\n\tfrom ..p.a import x\n",
		},
	)
	assert cycles == [[f"{PKG}.p.a", f"{PKG}.q.b"]]


def test_a_self_import_is_a_cycle(tmp_path):
	assert found(tmp_path, {"a.py": "def f():\n\tfrom .a import f\n"}) == [[f"{PKG}.a"]]


def test_third_party_and_stdlib_imports_are_ignored(tmp_path):
	assert found(tmp_path, {"a.py": "import os\nimport frappe\nfrom decimal import Decimal\n"}) == []


def test_compare_is_shrink_only():
	new, stale = cic.compare([["a", "b"]], [["c", "d"]])
	assert new == [["a", "b"]] and stale == [["c", "d"]]
	assert cic.compare([], []) == ([], [])


def test_update_refuses_to_add_a_cycle(tmp_path, monkeypatch, capsys):
	files = {"a.py": "from .b import y\n", "b.py": "from .a import x\n"}
	make_package(tmp_path, files)
	baseline = tmp_path / "baseline.json"
	baseline.write_text("[]\n", encoding="utf-8")
	monkeypatch.setattr(cic, "PRODUCT", tmp_path)
	monkeypatch.setattr(cic, "BASELINE", baseline)
	assert cic.main(["--update"]) == 1
	assert baseline.read_text(encoding="utf-8") == "[]\n"
	assert cic.main([]) == 1
	assert "NEW cycle" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["not json", '{"a": 1}', '[["a", 1]]'])
def test_unreadable_baseline_exits(tmp_path, bad):
	path = tmp_path / "b.json"
	path.write_text(bad, encoding="utf-8")
	with pytest.raises(SystemExit):
		cic.load_baseline(path)
