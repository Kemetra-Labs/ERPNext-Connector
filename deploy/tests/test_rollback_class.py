"""RT-927 Task 2: the rollback class (image-only versus restore) between two Connector trees."""

import json
import subprocess
from pathlib import Path

import pytest
import rollback_class as rc

SUBTREE = "integrations/erpnext-connector"
PKG = "retail_tower_erpnext_connector"
DOCTYPE = f"{PKG}/connector/doctype/connector_settings/connector_settings.json"

# The Connector root: git finds the monorepo from here, and `<sha>:<path>` is root-relative anyway.
CONNECTOR_ROOT = Path(__file__).resolve().parents[2]


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    return proc.stdout.strip()


def _doctype(fields: list[dict], **top) -> str:
    body = {"doctype": "DocType", "name": "Connector Settings", "issingle": 1,
            "modified": "2026-01-01", "field_order": [f["fieldname"] for f in fields], "fields": fields}
    body.update(top)
    return json.dumps(body, indent=1, sort_keys=True)


def _field(name: str, fieldtype: str = "Data", **extra) -> dict:
    return {"fieldname": name, "fieldtype": fieldtype, "label": name, **extra}


class Repo:
    def __init__(self, path: Path):
        self.path = path
        _git(path, "init", "-q", "-b", "main")
        self.files: dict[str, str] = {}

    def commit(self, **changes: str | None) -> str:
        """Write files under the subtree (keys use `__` for `/`) and return the commit SHA."""
        for key, value in changes.items():
            rel = f"{SUBTREE}/{key.replace('__', '/')}"
            target = self.path / rel
            if value is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(value, encoding="utf-8", newline="\n")
        _git(self.path, "add", "-A")
        _git(self.path, "commit", "-q", "--allow-empty", "-m", "c")
        return _git(self.path, "rev-parse", "HEAD")


def _base(tmp_path: Path, **extra: str) -> tuple[Repo, str]:
    repo = Repo(tmp_path)
    files = {
        f"{PKG}__patches.txt": "",
        f"{PKG}__hooks.py": "scheduler_events = {}\n",
        f"{PKG}__connector__schema.py": "X = 1\n",
        f"{PKG}__connector__doctype__connector_settings__connector_settings.json": _doctype([_field("a")]),
        f"{PKG}__connector__doctype__connector_settings__connector_settings.py": "class C: ...\n",
        "docs__note.md": "one\n",
    }
    files.update(extra)
    return repo, repo.commit(**files)


def _klass(repo: Repo, current: str, target: str) -> rc.Result:
    return rc.classify(str(repo.path), current, target)


def test_single_optional_field_is_image_only(tmp_path):
    repo, cur = _base(tmp_path)
    tgt = repo.commit(**{f"{PKG}__connector__doctype__connector_settings__connector_settings.json":
                         _doctype([_field("a"), _field("b", reqd=0)], modified="2026-02-02")})
    assert _klass(repo, cur, tgt).klass == "image-only"


def test_non_single_optional_field_is_image_only(tmp_path):
    repo, cur = _base(tmp_path)
    key = f"{PKG}__connector__doctype__posting_log__posting_log.json"
    cur = repo.commit(**{key: _doctype([_field("a")], issingle=0)})
    tgt = repo.commit(**{key: _doctype([_field("a"), _field("b")], issingle=0)})
    assert _klass(repo, cur, tgt).klass == "image-only"


def test_required_field_is_restore(tmp_path):
    repo, cur = _base(tmp_path)
    tgt = repo.commit(**{f"{PKG}__connector__doctype__connector_settings__connector_settings.json":
                         _doctype([_field("a"), _field("b", reqd=1)])})
    result = _klass(repo, cur, tgt)
    assert result.klass == "restore" and any("reqd" in r for r in result.reasons)


def test_unique_field_is_restore(tmp_path):
    repo, cur = _base(tmp_path)
    tgt = repo.commit(**{f"{PKG}__connector__doctype__connector_settings__connector_settings.json":
                         _doctype([_field("a"), _field("b", unique=1)])})
    assert _klass(repo, cur, tgt).klass == "restore"


def test_changed_fieldtype_is_restore(tmp_path):
    repo, cur = _base(tmp_path)
    tgt = repo.commit(**{f"{PKG}__connector__doctype__connector_settings__connector_settings.json":
                         _doctype([_field("a", "Int")])})
    assert _klass(repo, cur, tgt).klass == "restore"


def test_removed_field_is_restore(tmp_path):
    repo, cur = _base(tmp_path)
    tgt = repo.commit(**{f"{PKG}__connector__doctype__connector_settings__connector_settings.json":
                         _doctype([])})
    assert _klass(repo, cur, tgt).klass == "restore"


def test_top_level_doctype_property_change_is_restore(tmp_path):
    repo, cur = _base(tmp_path)
    tgt = repo.commit(**{f"{PKG}__connector__doctype__connector_settings__connector_settings.json":
                         _doctype([_field("a")], autoname="hash")})
    assert _klass(repo, cur, tgt).klass == "restore"


def test_new_and_removed_doctype_are_restore(tmp_path):
    repo, cur = _base(tmp_path)
    key = f"{PKG}__connector__doctype__new_one__new_one.json"
    added = repo.commit(**{key: _doctype([_field("a")], issingle=0)})
    assert _klass(repo, cur, added).klass == "restore"
    removed = repo.commit(**{key: None})
    assert _klass(repo, added, removed).klass == "restore"


def test_unparseable_doctype_json_is_restore(tmp_path):
    repo, cur = _base(tmp_path)
    tgt = repo.commit(**{f"{PKG}__connector__doctype__connector_settings__connector_settings.json": "{"})
    assert _klass(repo, cur, tgt).klass == "restore"


def test_schema_py_is_restore(tmp_path):
    repo, cur = _base(tmp_path)
    tgt = repo.commit(**{f"{PKG}__connector__schema.py": "X = 2\n"})
    result = _klass(repo, cur, tgt)
    assert result.klass == "restore" and any("schema.py" in r for r in result.reasons)


def test_patch_body_is_restore(tmp_path):
    repo, cur = _base(tmp_path)
    cur = repo.commit(**{f"{PKG}__patches__x.py": "A = 1\n"})
    tgt = repo.commit(**{f"{PKG}__patches__x.py": "A = 2\n"})
    result = _klass(repo, cur, tgt)
    assert result.klass == "restore" and any("patches/x.py" in r for r in result.reasons)


def test_patches_txt_is_restore(tmp_path):
    repo, cur = _base(tmp_path)
    tgt = repo.commit(**{f"{PKG}__patches.txt": "retail_tower_erpnext_connector.patches.x\n"})
    assert _klass(repo, cur, tgt).klass == "restore"


@pytest.mark.parametrize("key", [
    f"{PKG}__connector__doctype__connector_settings__connector_settings.py",
    f"{PKG}__install.py",
    f"{PKG}__setup__install_extra.py",
    f"{PKG}__fixtures__custom_field.json",
])
def test_other_restore_triggers(tmp_path, key):
    repo, cur = _base(tmp_path)
    tgt = repo.commit(**{key: "changed = 1\n"})
    assert _klass(repo, cur, tgt).klass == "restore"


def test_hooks_scheduler_entry_is_image_only(tmp_path):
    repo, cur = _base(tmp_path)
    tgt = repo.commit(**{f"{PKG}__hooks.py": "scheduler_events = {'cron': {'* * * * *': ['x.run']}}\n"})
    result = _klass(repo, cur, tgt)
    assert result.klass == "image-only" and result.reasons == ("no schema-affecting change",)


def test_docs_only_change_is_image_only(tmp_path):
    repo, cur = _base(tmp_path)
    tgt = repo.commit(**{"docs__note.md": "two\n"})
    assert _klass(repo, cur, tgt).klass == "image-only"


def test_same_commit_is_image_only(tmp_path):
    repo, cur = _base(tmp_path)
    assert _klass(repo, cur, cur).klass == "image-only"


def test_changes_outside_the_subtree_are_ignored(tmp_path):
    repo, cur = _base(tmp_path)
    (tmp_path / "other.txt").write_text("x", encoding="utf-8")
    _git(tmp_path, "add", "other.txt")
    _git(tmp_path, "commit", "-q", "-m", "other")
    tgt = _git(tmp_path, "rev-parse", "HEAD")
    assert _klass(repo, cur, tgt).klass == "image-only"


@pytest.mark.parametrize("bad", ["abc1234", "HEAD", "main", "g" * 40, "A" * 40])
def test_only_full_lowercase_hex_shas_are_accepted(tmp_path, bad):
    repo, cur = _base(tmp_path)
    with pytest.raises(rc.InputError):
        rc.classify(str(repo.path), bad, cur)
    with pytest.raises(rc.InputError):
        rc.classify(str(repo.path), cur, bad)


def test_unknown_sha_and_missing_subtree_raise(tmp_path):
    repo, cur = _base(tmp_path)
    with pytest.raises(rc.InputError):
        rc.classify(str(repo.path), "1" * 40, cur)
    (tmp_path / "bare.txt").write_text("x", encoding="utf-8")
    _git(tmp_path, "rm", "-rq", SUBTREE)
    _git(tmp_path, "add", "bare.txt")
    _git(tmp_path, "commit", "-q", "-m", "no subtree")
    empty = _git(tmp_path, "rev-parse", "HEAD")
    with pytest.raises(rc.InputError):
        rc.classify(str(repo.path), cur, empty)


def test_repo_argument_may_be_a_subdirectory(tmp_path):
    """git resolves a bare pathspec against the cwd; a subdirectory --repo must not hide the diff."""
    repo, cur = _base(tmp_path)
    tgt = repo.commit(**{f"{PKG}__connector__schema.py": "X = 2\n"})
    assert rc.classify(str(tmp_path / SUBTREE), cur, tgt).klass == "restore"


def test_cli_prints_rt_lines(tmp_path, capsys):
    repo, cur = _base(tmp_path)
    tgt = repo.commit(**{f"{PKG}__connector__schema.py": "X = 2\n"})
    assert rc.main(["--repo", str(repo.path), "--current", cur, "--target", tgt]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "RT_ROLLBACK_CLASS=restore"
    assert all(line.startswith("RT_ROLLBACK_REASON=") for line in lines[1:]) and len(lines) >= 2


def test_cli_exit_2_on_unknown_sha(tmp_path, capsys):
    repo, cur = _base(tmp_path)
    assert rc.main(["--repo", str(repo.path), "--current", cur, "--target", "2" * 40]) == 2
    assert capsys.readouterr().out == ""


def test_real_first_move():
    """a3b31ff7 (what lab-03 runs) -> this commit: connector_settings.py changed, so restore."""
    try:
        current = _git(CONNECTOR_ROOT, "rev-parse", "--verify", "a3b31ff7^{commit}")
        target = _git(CONNECTOR_ROOT, "rev-parse", "--verify", "HEAD")
        _git(CONNECTOR_ROOT, "cat-file", "-e", f"{current}:{SUBTREE}")
    except subprocess.CalledProcessError:
        pytest.skip("a3b31ff7 is not in this checkout (shallow clone)")
    result = rc.classify(str(CONNECTOR_ROOT), current, target)
    assert result.klass == "restore"
    assert any("connector_settings.py" in r for r in result.reasons)
    # The real DocType JSON parses at both commits: a parse failure here would be the tool, not the change.
    assert not any("could not be parsed" in r for r in result.reasons), result.reasons


def test_hooks_change_outside_the_scheduler_is_restore(tmp_path):
    """fixtures / after_migrate in hooks.py change the database at migrate (review I2)."""
    repo, cur = _base(tmp_path)
    tgt = repo.commit(**{f"{PKG}__hooks.py": "scheduler_events = {}\nfixtures = ['Custom Field']\n"})
    result = _klass(repo, cur, tgt)
    assert result.klass == "restore" and any("hooks.py" in r for r in result.reasons)


def test_hooks_that_cannot_be_parsed_are_restore(tmp_path):
    repo, cur = _base(tmp_path)
    tgt = repo.commit(**{f"{PKG}__hooks.py": "scheduler_events = {\n"})
    assert _klass(repo, cur, tgt).klass == "restore"


def test_module_custom_json_is_restore(tmp_path):
    repo, cur = _base(tmp_path)
    tgt = repo.commit(**{f"{PKG}__connector__custom__sales_invoice.json": "{}"})
    assert _klass(repo, cur, tgt).klass == "restore"
