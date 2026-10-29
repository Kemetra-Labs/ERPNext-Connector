"""RT-927 Task 1: the ERP lab image recipe (static checks, stdlib only).

The build workflow and the graph tier are root files; their checks are in
.github/release/test_erp_lab_workflows.py (a product's tests do not read outside the product).
"""

import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[1] / "erp.Dockerfile"
BASE_DIGEST = "sha256:f2d409c58ed1b688b6f2914d4a7f8b2468ee60bd1a3f1bd82d6fe32071806cb1"


def _text() -> str:
    # The repo index is LF; a Windows checkout may be CRLF. Compare on LF.
    return DOCKERFILE.read_text(encoding="utf-8").replace("\r\n", "\n")


def _from_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("FROM ")]


def test_base_pinned_by_digest():
    text = _text()
    assert _from_lines(text)[0] == f"FROM frappe/erpnext@{BASE_DIGEST}"
    assert ":v16" not in text
    assert len(_from_lines(text)) == 1, "a single stage, no second base"


def test_copy_owner_and_target():
    assert re.search(r"^COPY --chown=1000:0 \S+ apps/retail_tower_erpnext_connector$", _text(), re.M)


def test_installs_into_the_bench_env_with_a_uv_fallback():
    text = _text()
    assert "./env/bin/pip install --no-cache-dir -e apps/retail_tower_erpnext_connector" in text
    assert "uv pip install --python ./env/bin/python -e apps/retail_tower_erpnext_connector" in text
    assert "print(m.__version__)" in text


def test_labels_declared():
    text = _text()
    for key in ("org.opencontainers.image.revision", "rt.connector.tree", "rt.connector.version", "rt.base.digest"):
        assert key in text, key
    assert f'rt.base.digest="{BASE_DIGEST}"' in text


def test_runs_as_the_frappe_user_in_the_bench():
    text = _text()
    assert "USER frappe" in text
    assert "WORKDIR /home/frappe/frappe-bench" in text
