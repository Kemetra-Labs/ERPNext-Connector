"""RT-927: decide whether a Connector rollback may be image-only or needs a database restore.

Design RT-926 v2, section 5 (Confluence 60391425). The deploy preflight compares the Connector
tree of the release lab-03 runs now (`current`) with the one it is about to deploy (`target`):

* restore     a schema-affecting file changed, so migrate may have changed the database and an
              older image cannot be put back over it. Only the owner-run restore script (before
              the scheduler resumes) can undo it.
* image-only  nothing schema-affecting changed, so re-tagging the previous image is enough.

The rule is fail-closed: anything it cannot parse is `restore`. Both inputs are full 40-hex
commit SHAs; a branch name or a short SHA is refused, so the answer can never depend on a ref
that moved.

CLI: python rollback_class.py --repo R --current C --target T
  prints RT_ROLLBACK_CLASS=<klass> and one RT_ROLLBACK_REASON=<text> per reason, exit 0;
  exit 2 (nothing on stdout) on an unknown or non-full SHA or a missing subtree.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Literal

SUBTREE = "integrations/erpnext-connector"
PKG = "retail_tower_erpnext_connector"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
NO_CHANGE_REASON = "no schema-affecting change"

# Paths are relative to the Connector root. Each trigger is (glob, why).
RESTORE_GLOBS: tuple[tuple[str, str], ...] = (
    (f"{PKG}/patches.txt", "patches.txt changed"),
    (f"{PKG}/patches/*", "a patch changed"),
    (f"{PKG}/connector/schema.py", "connector/schema.py changed"),
    ("*/doctype/*.py", "a DocType controller changed"),
    (f"{PKG}/fixtures/*", "a fixture changed"),
    ("*/custom/*.json", "a module customization changed"),
)
HOOKS = f"{PKG}/hooks.py"
# The one hooks.py name whose change has no schema effect (design section 5: scheduler entries).
SCHEDULER_HOOK = "scheduler_events"
# Top-level DocType keys that may differ without a schema effect.
IGNORED_DOCTYPE_KEYS = frozenset({"modified", "modified_by", "creation", "field_order", "fields"})


class InputError(ValueError):
    """A SHA is not a full 40-hex commit, or the subtree is missing at one of them."""


@dataclass(frozen=True)
class Result:
    klass: Literal["image-only", "restore"]
    reasons: tuple[str, ...]


def _git(repo: str, *args: str) -> bytes:
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, check=False)
    if proc.returncode != 0:
        raise InputError(proc.stderr.decode("utf-8", "replace").strip() or "git failed")
    return proc.stdout


def _blob(repo: str, sha: str, path: str) -> bytes:
    # cat-file never treats its argument as a file name; `git show <sha>:<path>` from a subdirectory
    # first tries to stat it (on Windows that fails with "Filename too long").
    return _git(repo, "cat-file", "blob", f"{sha}:{path}")


def _check_commit(repo: str, sha: str, subtree: str) -> None:
    if not SHA_RE.fullmatch(sha):
        raise InputError(f"'{sha}' is not a full 40-hex lowercase commit SHA")
    _git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
    _git(repo, "cat-file", "-e", f"{sha}:{subtree}")


def _changes(repo: str, current: str, target: str, subtree: str) -> list[tuple[str, str]]:
    # :(top) anchors the pathspec at the repository root: a bare one is read relative to the cwd, and
    # `--repo <subdirectory>` would then match nothing and wrongly answer image-only.
    raw = _git(repo, "diff", "--name-status", "--no-renames", "-z", current, target, "--", f":(top){subtree}")
    parts = raw.decode("utf-8").split("\0")
    out = []
    for status, path in zip(parts[0::2], parts[1::2], strict=False):
        if status and path:
            out.append((status[0], path.removeprefix(subtree + "/")))
    return out


def _show_json(repo: str, sha: str, path: str) -> dict | None:
    try:
        data = json.loads(_blob(repo, sha, path).decode("utf-8"))
    except (InputError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _is_doctype_json(path: str) -> bool:
    return fnmatch.fnmatchcase(path, "*/doctype/*/*.json")


def _hooks_statements(source: str) -> dict[str, str]:
    """hooks.py top level, as name -> dumped AST; unnamed statements get a positional key."""
    out: dict[str, str] = {}
    for index, node in enumerate(ast.parse(source).body):
        targets = node.targets if isinstance(node, ast.Assign) else []
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        key = names[0] if len(names) == 1 else f"<statement {index}>"
        out[key] = ast.dump(node.value if names else node)
    return out


def _hooks_reasons(repo: str, current: str, target: str, subtree: str, status: str) -> list[str]:
    """hooks.py also declares fixtures and after_install/after_migrate, which change the database at
    migrate. Only a change limited to scheduler_events is image-only; anything else, or a file that
    cannot be parsed, is restore."""
    if status != "M":
        return [f"hooks.py was {'added' if status == 'A' else 'removed'}"]
    try:
        old = _hooks_statements(_blob(repo, current, f"{subtree}/{HOOKS}").decode("utf-8"))
        new = _hooks_statements(_blob(repo, target, f"{subtree}/{HOOKS}").decode("utf-8"))
    except (InputError, SyntaxError, ValueError):
        return ["hooks.py could not be parsed"]
    changed = sorted(k for k in set(old) | set(new) if k != SCHEDULER_HOOK and old.get(k) != new.get(k))
    return [f"hooks.py changed outside {SCHEDULER_HOOK}: {', '.join(changed)}"] if changed else []


def _is_install(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.startswith("install") and name.endswith(".py")


def _doctype_reasons(repo: str, current: str, target: str, subtree: str, status: str, path: str) -> list[str]:
    if status != "M":
        return [f"DocType {path} was {'added' if status == 'A' else 'removed'}"]
    old = _show_json(repo, current, f"{subtree}/{path}")
    new = _show_json(repo, target, f"{subtree}/{path}")
    if old is None or new is None:
        return [f"DocType {path} could not be parsed"]
    reasons: list[str] = []
    for key in sorted((set(old) | set(new)) - IGNORED_DOCTYPE_KEYS):
        if old.get(key) != new.get(key):
            reasons.append(f"DocType {path}: property '{key}' changed")
    old_fields = {f.get("fieldname"): f for f in old.get("fields", [])}
    new_fields = {f.get("fieldname"): f for f in new.get("fields", [])}
    for name in sorted(map(str, old_fields.keys() - new_fields.keys())):
        reasons.append(f"DocType {path}: field '{name}' was removed")
    for name in sorted(map(str, old_fields.keys() & new_fields.keys())):
        if old_fields[name] != new_fields[name]:
            reasons.append(f"DocType {path}: field '{name}' changed")
    for name in sorted(map(str, new_fields.keys() - old_fields.keys())):
        field = new_fields[name]
        if field.get("reqd") or field.get("unique"):
            flag = "reqd" if field.get("reqd") else "unique"
            reasons.append(f"DocType {path}: added field '{name}' is {flag}")
    return reasons


def _added_optional_fields(repo: str, current: str, target: str, subtree: str, path: str) -> list[str]:
    old = _show_json(repo, current, f"{subtree}/{path}") or {}
    new = _show_json(repo, target, f"{subtree}/{path}") or {}
    old_names = {f.get("fieldname") for f in old.get("fields", [])}
    return [str(f.get("fieldname")) for f in new.get("fields", []) if f.get("fieldname") not in old_names]


def classify(repo: str, current: str, target: str, subtree: str = SUBTREE) -> Result:
    _check_commit(repo, current, subtree)
    _check_commit(repo, target, subtree)
    restore: list[str] = []
    optional: list[str] = []
    for status, path in _changes(repo, current, target, subtree):
        if _is_doctype_json(path):
            found = _doctype_reasons(repo, current, target, subtree, status, path)
            restore.extend(found)
            if not found and status == "M":
                added = _added_optional_fields(repo, current, target, subtree, path)
                if added:
                    optional.append(f"optional fields added to {path}: {', '.join(added)}")
            continue
        if path == HOOKS:
            restore.extend(_hooks_reasons(repo, current, target, subtree, status))
            continue
        why = next((why for glob, why in RESTORE_GLOBS if fnmatch.fnmatchcase(path, glob)), None)
        if why is None and _is_install(path):
            why = "an install script changed"
        if why:
            restore.append(f"{why}: {path}")
    if restore:
        return Result("restore", tuple(restore))
    return Result("image-only", tuple(optional) or (NO_CHANGE_REASON,))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--current", required=True)
    parser.add_argument("--target", required=True)
    args = parser.parse_args(argv)
    try:
        result = classify(args.repo, args.current, args.target)
    except InputError as exc:
        print(f"rollback_class: {exc}", file=sys.stderr)
        return 2
    print(f"RT_ROLLBACK_CLASS={result.klass}")
    for reason in result.reasons:
        print("RT_ROLLBACK_REASON=" + " ".join(reason.split()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
