# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Posting page processor — the re-P2b hybrid isolate + raise-after loop (Task A).

Per the ratified re-P2b decision: a feed page line missing ``erpnextItemRef`` is an upstream
DP2 contract violation (rider R2). The connector:

  - treats it as a **terminal validation rejection for that work item only**
    (`permanently_rejected` / `validation` — the wire category is the closed-set `validation`;
    the specificity rides in the reason ``message``);
  - **never** resolves, searches, creates, or substitutes an ERPNext Item;
  - **continues** processing the valid work items on the same page;
  - records every per-item outcome FIRST, then **raises a page-degraded alert AFTER** so the DP2
    feed contract can be fixed — never an abort that strands valid or already-acked items;
  - relies on the per-outcome ack key + the idempotency store to make a scheduler retry safe.

This module imports NO frappe — it is pure orchestration over the injected client + poster.
"""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import OutcomeAckRequest
from .reasons import FailureKind, to_rejection_reason
from .transport import AckConflict, AckNotFound, PostingFeedClient

# Operational: a page that processed but contained ≥1 contract-violating item. Raised AFTER all
# per-item outcomes are acked, so it signals "feed is degraded, fix upstream" without losing work.
_MISSING_REF_MESSAGE = (
    "feed page contained {n} line(s) missing erpnextItemRef (upstream DP2 contract violation, "
    "rider R2) — rejected per-item: {refs}"
)


class PageDegraded(Exception):
    """Operational-alert type for a degraded page (carried ≥1 contract-violating item).

    NOT raised by :func:`process_page` — that returns a ``PageResult`` with ``degraded=True`` and
    advances the cursor (every item reached a terminal outcome; re-pulling is the forbidden retry).
    This type exists so the caller can surface the breach (log/alert) with a typed payload.
    """


@dataclass(frozen=True)
class PageResult:
    """Outcome of processing one pull page.

    ``degraded`` is True when the page carried ≥1 contract-violating item (acked
    `permanently_rejected`) OR ≥1 item whose ack DP2 rejected (409/404 — `ack_failed_refs`). The
    cursor always advances on a degraded page — every item reached a terminal disposition, so
    re-pulling would be a forbidden retry (re-P2b). ``degraded_message`` is the operational-alert
    text the caller logs (None when not degraded). ``ack_failed_refs`` are posted-but-ack-rejected
    items that DP2's reconciliation (017) owns — the connector does not retry them.
    """

    cursor: str
    next_page_token: str | None
    posted_count: int
    rejected_refs: tuple[str, ...]
    degraded: bool
    degraded_message: str | None = None
    ack_failed_refs: tuple[str, ...] = ()


def process_page(
    client: PostingFeedClient,
    ack_client,
    *,
    since: str | None,
    post_valid,
) -> PageResult:
    """Process one pull page with re-P2b isolation.

    ``client.pull_postings_raw`` splits the page into valid items + isolated invalid ones.
    ``post_valid(work_item)`` posts a valid item (the caller injects the real posting path —
    e.g. ``frappe_glue.post_work_item`` bound with its deps, or a fake in tests). ``ack_client``
    acks the invalid items (same interface as :class:`PostingFeedClient`).

    Returns a :class:`PageResult`; raises :class:`PageDegraded` AFTER recording all outcomes if
    the page carried any invalid item.
    """
    page = client.pull_postings_raw(since=since)

    # 1. Post every valid work item (the happy path; each is independently idempotent).
    #    An AckConflict (409) / AckNotFound (404) from a valid item's ack is ISOLATED, not fatal:
    #    DP2 owns reconciliation (017); the connector must NOT blind-retry or let one item wedge the
    #    loop (the same poison-pill class as a degraded page). Collect the ref, mark degraded, go on
    #    — every item still reaches a terminal disposition, so the cursor advances.
    ack_failed_refs: list[str] = []
    posted_count = 0
    for work_item in page.items:
        try:
            post_valid(work_item)
            posted_count += 1
        except (AckConflict, AckNotFound):
            # isolate: the item posted (or replayed) but its ack was rejected by DP2 → a
            # reconciliation case (017), not a connector retry. Record the ref and continue.
            ack_failed_refs.append(work_item.work_item_ref)

    # 2. Ack each invalid item as a terminal per-item rejection — no substitution, no resolution.
    rejected_refs: list[str] = []
    for bad in page.invalid:
        # Closed 012 set: `validation` (no `missing_erpnext_item_ref` wire category). RT-409: the parse
        # error echoes raw feed values, so the message is scrubbed and bounded like every other reason;
        # an over-long one would be refused (400) and stall the page.
        reason = to_rejection_reason(FailureKind.VALIDATION, message=f"{bad.error_kind}: {bad.message}")
        # RT-333: echo (and key on) the item's resolution version when it is still readable, so
        # Backend-Core can fence a rejection of a superseded version like any other attempt.
        version = _raw_resolution_version(bad.raw)
        key = f"{bad.work_item_ref}:permanently_rejected"
        ack_client.ack_outcome(
            bad.work_item_ref,
            OutcomeAckRequest.permanently_rejected(reason, resolution_version=version),
            idempotency_key=key if version is None else f"{key}:v{version}",
        )
        rejected_refs.append(bad.work_item_ref)

    # 3. Return a result carrying `degraded` + the advanced cursor. We do NOT raise: every item
    #    on the page reached a terminal outcome (posted or acked-rejected), so the cursor MUST
    #    advance — re-pulling would be the forbidden page-level retry of already-acked items
    #    (re-P2b: "no page-level retry... emits an operational alert"). The caller logs the alert
    #    and advances. ``degraded_message`` is the alert text when degraded.
    degraded = bool(page.invalid) or bool(ack_failed_refs)
    parts: list[str] = []
    if page.invalid:
        parts.append(_MISSING_REF_MESSAGE.format(n=len(rejected_refs), refs=", ".join(rejected_refs)))
    if ack_failed_refs:
        parts.append(
            f"{len(ack_failed_refs)} item(s) posted but their ack was rejected by DP2 "
            f"(409/404 — reconciliation case 017, NOT retried): {', '.join(ack_failed_refs)}"
        )
    return PageResult(
        cursor=page.cursor,
        next_page_token=page.next_page_token,
        posted_count=posted_count,
        rejected_refs=tuple(rejected_refs),
        ack_failed_refs=tuple(ack_failed_refs),
        degraded=degraded,
        degraded_message="; ".join(parts) if parts else None,
    )


def _raw_resolution_version(raw: dict) -> int | None:
    """The malformed item's ``resolutionVersion`` when it is a positive integer, else ``None``."""
    version = raw.get("resolutionVersion") if isinstance(raw, dict) else None
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return None
    return version
