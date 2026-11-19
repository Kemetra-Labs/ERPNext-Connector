# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Gate G5 index guard — the pure "may we post?" decision (RT-58).

Posting is exactly-once only while BOTH composite unique indexes exist:

  - ``unique_rt_si_provenance`` on Sales Invoice ``(rt_source_system, rt_external_id)`` — the
    crash-window dedup (F-002): a second submit of an already-posted sale fails at the DB;
  - ``unique_rt_posting_idem`` on Posting Log ``(source_system, external_id)`` — the connector's own
    replay record.

RT-54 proved they can be silently absent: a fresh ``install-app`` marks every patch completed
without running it, and a crash-window replay then duplicated the Sales Invoice AND (since RT-48)
its stock movement. The poller asks this module before every tick. Anything other than "both
present as the exact UNIQUE constraint" — a missing index, a same-named non-unique or wrong-column
index (Codex P1, PR #43), or an unreadable schema — means **do not post** (sales stay pending in
DP2; nothing is rejected, lost or duplicated). This module imports NO frappe; the glue supplies the
``index_present(doctype, index_name, columns)`` probe and the ``SHOW INDEX`` rows.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

G5_INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
	("Sales Invoice", "unique_rt_si_provenance", ("rt_source_system", "rt_external_id")),
	("Posting Log", "unique_rt_posting_idem", ("source_system", "external_id")),
)


# RT-1422: the count leg's own exactly-once index. It is NOT part of G5_INDEXES: a site without it must
# keep posting sales and reversals, and only pause `inventory_count` items (``failed_transient``).
COUNT_INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
	("Stock Reconciliation", "unique_rt_sr_provenance", ("rt_source_system", "rt_external_id")),
)


# RT-1514: the transfer legs' own exactly-once index. NOT part of G5_INDEXES or COUNT_INDEXES: a site without it
# must keep posting sales, reversals and counts, and only pause the two transfer kinds (``failed_transient``).
TRANSFER_INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
	("Stock Entry", "unique_rt_se_provenance", ("rt_source_system", "rt_external_id")),
)


@dataclass(frozen=True)
class GuardResult:
	"""``ok`` only when every required index is present; ``missing`` names the absent ones."""

	ok: bool
	missing: tuple[str, ...] = ()
	error: str | None = None


def duplicate_provenance_message(rows: Sequence[Mapping[str, object]]) -> str:
	"""The fail-closed data-preflight error of the Stock Reconciliation index patch (RT-1422)."""
	sample = ", ".join(
		f"({r.get('rt_source_system')}, {r.get('rt_external_id')}) x{r.get('n')}" for r in list(rows)[:5]
	)
	return (
		"stock_reconciliation_unique_provenance: duplicate provenance keys exist on Stock Reconciliation "
		f"({len(rows)} key(s); first: {sample}); the unique index was NOT created and the count leg stays "
		"paused. Resolve them (never edit or cancel a document to hide a mismatch) and re-run migrate."
	)


def index_matches(rows: Sequence[Mapping[str, object]], columns: Sequence[str]) -> bool:
	"""True only if ``SHOW INDEX`` rows for one key describe a UNIQUE index on exactly ``columns``, in order."""
	if not rows:
		return False
	if not _all_unique(rows):
		return False
	ordered = sorted(rows, key=lambda r: int(r["Seq_in_index"]))  # type: ignore[arg-type]
	return tuple(str(r["Column_name"]) for r in ordered) == tuple(columns)


def _all_unique(rows: Sequence[Mapping[str, object]]) -> bool:
	"""Every part is UNIQUE and covers the FULL column (a prefix ``Sub_part`` would collide ids)."""
	return all(int(r.get("Non_unique") or 0) == 0 and r.get("Sub_part") is None for r in rows)


def evaluate(
	index_present: Callable[[str, str, tuple[str, ...]], object],
	required: tuple[tuple[str, str, tuple[str, ...]], ...] = G5_INDEXES,
) -> GuardResult:
	"""Decide whether posting may proceed. A probe failure is NOT ok (never assume the index exists)."""
	missing: list[str] = []
	for doctype, index, columns in required:
		try:
			present = bool(index_present(doctype, index, columns))
		except Exception as exc:  # an unreadable schema must block posting, not wave it through
			return GuardResult(ok=False, missing=tuple(missing), error=f"{type(exc).__name__}: {exc}"[:300])
		if not present:
			missing.append(f"{doctype}.{index}")
	return GuardResult(ok=not missing, missing=tuple(missing))
