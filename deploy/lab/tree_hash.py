"""RT-927: the git tree hash of an installed Connector directory, standard library only.

The ERP lab deploy needs to know that the Connector inside the running backend container is
the tree the operator declared as `CurrentTree`: the rollback class is computed between two
declared trees, so a host-built image that differs from its declaration would give a wrong
class. A host-built image has no `rt.connector.tree` label (images built by build-erp-image.yml
do), so the deploy hashes the installed directory and compares it with
`git rev-parse <sha>:integrations/erpnext-connector`.

This reproduces git's own tree hash for what `git archive` extracts: blobs, trees, the
executable bit and symlinks. It leaves out what installing the app adds to the directory:
`__pycache__`, `*.pyc` and `*.egg-info`, and a `.git` directory (never part of a git tree; a host
build that copied a clone carries one). Any other extra or missing file changes the hash,
which is the point: a mismatch stops the deploy before anything changes.

Usage: python tree_hash.py DIR    (prints the 40-hex hash; exit 2 when DIR is not a directory)
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path

IGNORED_DIRS = ("__pycache__", ".git")  # .git: a host build that copied a clone (lab-03)
IGNORED_DIR_SUFFIXES = (".egg-info",)
IGNORED_FILE_SUFFIXES = (".pyc",)


def _object(kind: bytes, body: bytes) -> bytes:
    return hashlib.sha1(kind + b" " + str(len(body)).encode() + b"\0" + body).digest()


def _ignored_dir(name: str) -> bool:
    return name in IGNORED_DIRS or name.endswith(IGNORED_DIR_SUFFIXES)


def _tree(path: Path) -> bytes | None:
    """The 20-byte tree id of `path`, or None when nothing in it is part of the tree."""
    entries: list[tuple[bytes, bytes, bytes]] = []  # (sort key, mode, name + id)
    with os.scandir(path) as it:
        for entry in it:
            name = entry.name.encode("utf-8")
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                body = os.readlink(entry.path).encode("utf-8")
                entries.append((name, b"120000", name + b"\0" + _object(b"blob", body)))
            elif stat.S_ISDIR(info.st_mode):
                if _ignored_dir(entry.name):
                    continue
                sub = _tree(Path(entry.path))
                if sub is not None:  # git cannot store an empty directory
                    entries.append((name + b"/", b"40000", name + b"\0" + sub))
            elif stat.S_ISREG(info.st_mode):
                if entry.name.endswith(IGNORED_FILE_SUFFIXES):
                    continue
                mode = b"100755" if info.st_mode & stat.S_IXUSR else b"100644"
                body = Path(entry.path).read_bytes()
                entries.append((name, mode, name + b"\0" + _object(b"blob", body)))
    if not entries:
        return None
    entries.sort(key=lambda item: item[0])
    return _object(b"tree", b"".join(mode + b" " + rest for _, mode, rest in entries))


def tree_hash(directory: Path) -> str:
    result = _tree(Path(directory))
    return (result if result is not None else _object(b"tree", b"")).hex()


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not Path(argv[0]).is_dir():
        print("usage: tree_hash.py DIR", file=sys.stderr)
        return 2
    print(tree_hash(Path(argv[0])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
