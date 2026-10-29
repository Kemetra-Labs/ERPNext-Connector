"""RT-927 Task 4 addition: tree_hash reproduces `git rev-parse <commit>:<dir>` for an installed tree."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import tree_hash


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
         "-c", "commit.gpgsign=false", *args],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    return proc.stdout.strip()


def _make(repo: Path, files: dict[str, str | bytes]) -> None:
    for rel, body in files.items():
        target = repo / "sub" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))


def _commit(repo: Path) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c")
    return _git(repo, "rev-parse", "HEAD:sub")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    return tmp_path


def test_matches_git_for_a_nested_tree(repo):
    _make(repo, {
        "a.py": "x = 1\n",
        "pkg/__init__.py": '__version__ = "0.3.0"\n',
        "pkg/mod/b.json": '{"k": 1}\n',
        "pkg/mod/zz": "z\n",
        "pkg-extra.txt": "sorts after pkg/ only because dirs compare as 'pkg/'\n",
        "empty.txt": "",
        "naïve.txt": "non-ascii name\n",
    })
    expected = _commit(repo)
    assert tree_hash.tree_hash(repo / "sub") == expected


def test_ignores_bytecode_and_egg_info_that_pip_adds(repo):
    _make(repo, {"pkg/__init__.py": "x = 1\n", "docs/readme.md": "r\n"})
    expected = _commit(repo)
    _make(repo, {
        "pkg/__pycache__/__init__.cpython-314.pyc": b"\x00\x01",
        "pkg/stray.pyc": b"\x00",
        "retail_tower.egg-info/PKG-INFO": "Name: x\n",
    })
    assert tree_hash.tree_hash(repo / "sub") == expected


def test_a_changed_file_changes_the_hash(repo):
    _make(repo, {"a.txt": "one\n"})
    expected = _commit(repo)
    (repo / "sub" / "a.txt").write_text("two\n", encoding="utf-8", newline="\n")
    assert tree_hash.tree_hash(repo / "sub") != expected


@pytest.mark.skipif(os.name == "nt", reason="the executable bit does not exist on Windows")
def test_executable_bit_is_part_of_the_hash(repo):
    _make(repo, {"run.sh": "#!/bin/sh\n"})
    (repo / "sub" / "run.sh").chmod(0o755)
    expected = _commit(repo)
    assert tree_hash.tree_hash(repo / "sub") == expected
    (repo / "sub" / "run.sh").chmod(0o644)
    assert tree_hash.tree_hash(repo / "sub") != expected


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlink_is_hashed_as_its_target(repo):
    _make(repo, {"real.txt": "r\n"})
    os.symlink("real.txt", repo / "sub" / "link.txt")
    expected = _commit(repo)
    assert tree_hash.tree_hash(repo / "sub") == expected


def test_cli_prints_only_the_hash(repo, capsys):
    _make(repo, {"a.txt": "one\n"})
    expected = _commit(repo)
    assert tree_hash.main([str(repo / "sub")]) == 0
    assert capsys.readouterr().out == expected + "\n"


def test_cli_exit_2_when_the_directory_is_missing(tmp_path, capsys):
    assert tree_hash.main([str(tmp_path / "nope")]) == 2
    assert capsys.readouterr().out == ""


def test_script_runs_with_a_bare_interpreter(repo):
    """The deploy runs it inside the container with only the standard library."""
    _make(repo, {"a.txt": "one\n"})
    expected = _commit(repo)
    script = Path(tree_hash.__file__)
    out = subprocess.run([sys.executable, "-I", str(script), str(repo / "sub")],
                         capture_output=True, text=True, check=True).stdout
    assert out == expected + "\n"
