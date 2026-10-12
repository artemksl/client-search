"""Outcome persistence and immutable, content-addressed approval (ADR-002 §6).

Three verification outcomes, each persisted; approval is a separate, human-only
step:

* **verified + unchanged** -> a dated snapshot record under ``snapshots/``; no
  notification.
* **verified + changed** -> a candidate under ``candidates/`` carrying the full
  proposed registry, the field-level diff and the verification evidence. Never
  auto-promoted; the candidate record itself plus the CLI exit status ARE the
  notification.
* **verification failure** -> the failed proposal persisted under ``candidates/``
  with ``is_change: null`` and the exact failing steps.

``approve(candidate_id, actor)`` is the ONLY path to a new approved version. It
writes an immutable, content-addressed file ``approved/<content-hash>.json`` with
the approving actor and timestamp. Approved files are never edited; a hash
mismatch on load (tampering, or a wrong-name file) is rejected loudly.

Consumer contract (wired in a later stage): an audit pins one approved registry
at run start via :meth:`Store.pinned` (by hash) or :meth:`Store.current_approved`.
Both re-verify the content hash before returning. **Stale or unreviewed facts must
make the dependent deterministic checks ``could_not_assess`` — never a silent
pass.** This module deliberately never decides ``isChange`` and never promotes a
candidate on its own.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from clinic_geo.crawler_registry import schema


OUTCOME_UNCHANGED = "unchanged"
OUTCOME_CHANGE = "change"
OUTCOME_FAILURE = "verification_failure"


class StoreError(RuntimeError):
    """A store invariant was violated (bad candidate, tampered approved file)."""


class ApprovalIntegrityError(StoreError):
    """An approved file's content hash does not match its address — rejected."""


@dataclass(frozen=True)
class OutcomeRecord:
    outcome: str
    candidate_id: str
    is_change: Optional[bool]
    path: str
    created_at: float


def _default_root() -> Path:
    from clinic_geo.paths import repo_root

    return repo_root() / "data" / "crawler-registry"


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


class Store:
    """Filesystem-backed outcome + approval store. No network, no model."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root).expanduser() if root is not None else _default_root()
        self.candidates_dir = self.root / "candidates"
        self.approved_dir = self.root / "approved"
        self.snapshots_dir = self.root / "snapshots"

    # -- outcome persistence ----------------------------------------------
    def record_unchanged(
        self, *, registry: dict, content_hash: str, created_at: float
    ) -> OutcomeRecord:
        path = self.snapshots_dir / f"{created_at:.0f}-{content_hash}.json"
        _write_json(
            path,
            {
                "outcome": OUTCOME_UNCHANGED,
                "is_change": False,
                "candidate_id": content_hash,
                "created_at": created_at,
                "registry": registry,
            },
        )
        return OutcomeRecord(
            OUTCOME_UNCHANGED, content_hash, False, str(path), created_at
        )

    def record_candidate(
        self,
        *,
        registry: dict,
        diff: Optional[dict],
        evidence: Optional[list],
        content_hash: str,
        created_at: float,
    ) -> OutcomeRecord:
        path = self.candidates_dir / f"{content_hash}.json"
        _write_json(
            path,
            {
                "outcome": OUTCOME_CHANGE,
                "is_change": True,
                "candidate_id": content_hash,
                "created_at": created_at,
                "registry": registry,
                "diff": diff,
                "evidence": evidence or [],
                "failing_steps": [],
            },
        )
        return OutcomeRecord(OUTCOME_CHANGE, content_hash, True, str(path), created_at)

    def record_failure(
        self,
        *,
        registry: Optional[dict],
        diff: Optional[dict],
        evidence: Optional[list],
        failing_steps: list,
        content_hash: Optional[str],
        created_at: float,
    ) -> OutcomeRecord:
        candidate_id = content_hash or hashlib.sha256(
            f"verification-failure:{created_at}".encode("utf-8")
        ).hexdigest()
        path = self.candidates_dir / f"{candidate_id}.json"
        _write_json(
            path,
            {
                "outcome": OUTCOME_FAILURE,
                "is_change": None,  # never collapsed to false
                "candidate_id": candidate_id,
                "created_at": created_at,
                "registry": registry,
                "diff": diff,
                "evidence": evidence or [],
                "failing_steps": failing_steps,
            },
        )
        return OutcomeRecord(OUTCOME_FAILURE, candidate_id, None, str(path), created_at)

    # -- candidate reads ---------------------------------------------------
    def list_candidates(self) -> list[dict]:
        if not self.candidates_dir.exists():
            return []
        records = []
        for path in self.candidates_dir.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            records.append(
                {
                    "candidate_id": data.get("candidate_id", path.stem),
                    "outcome": data.get("outcome"),
                    "is_change": data.get("is_change"),
                    "created_at": data.get("created_at"),
                    "failing_steps": data.get("failing_steps", []),
                }
            )
        return sorted(records, key=lambda r: (r.get("created_at") or 0, r["candidate_id"]))

    def show(self, candidate_id: str) -> dict:
        path = self.candidates_dir / f"{candidate_id}.json"
        if not path.exists():
            raise StoreError(f"no candidate {candidate_id!r}")
        return json.loads(path.read_text(encoding="utf-8"))

    # -- approval (the ONLY path to a new approved version) ----------------
    def approve(
        self, candidate_id: str, *, actor: str, timestamp: float
    ) -> dict:
        if not isinstance(actor, str) or not actor.strip():
            raise StoreError("approval requires a non-blank actor")
        candidate = self.show(candidate_id)
        if candidate.get("outcome") != OUTCOME_CHANGE or candidate.get("is_change") is not True:
            raise StoreError(
                f"candidate {candidate_id!r} is not an approvable change "
                f"(outcome={candidate.get('outcome')!r})"
            )
        registry = candidate.get("registry")
        # Re-validate and re-hash: approval must not enshrine a malformed or
        # mis-addressed proposal.
        schema.validate_registry(registry)
        recomputed = schema.content_hash(registry)
        if recomputed != candidate_id:
            raise ApprovalIntegrityError(
                f"candidate {candidate_id!r} content hash is {recomputed!r}; "
                "refusing to approve a mis-addressed candidate"
            )

        path = self.approved_dir / f"{recomputed}.json"
        if path.exists():
            # Content-addressed and immutable: an existing approval with the same
            # hash is the same content. Return it unchanged rather than rewriting.
            return self._load_approved(path)

        _write_json(
            path,
            {
                "content_hash": recomputed,
                "approved_by": actor.strip(),
                "approved_at": timestamp,
                "registry": registry,
            },
        )
        return self._load_approved(path)

    # -- approved accessors (hash-verified) --------------------------------
    def _load_approved(self, path: Path) -> dict:
        data = json.loads(path.read_text(encoding="utf-8"))
        registry = data.get("registry")
        stored_hash = data.get("content_hash")
        recomputed = schema.content_hash(registry)
        if recomputed != stored_hash or recomputed != path.stem:
            raise ApprovalIntegrityError(
                f"approved file {path.name} failed integrity check "
                f"(recomputed {recomputed!r}, stored {stored_hash!r})"
            )
        return data

    def pinned(self, content_hash: str) -> dict:
        """Return the approved registry for a hash, verifying integrity.

        This is the seam an audit uses to pin a registry at run start. A missing
        or tampered file raises rather than returning an untrustworthy registry.
        """

        path = self.approved_dir / f"{content_hash}.json"
        if not path.exists():
            raise StoreError(f"no approved registry {content_hash!r}")
        return self._load_approved(path)

    def current_approved(self) -> Optional[dict]:
        """The most recently approved version (by ``approved_at``), or ``None``.

        Every candidate file is integrity-checked on load; a tampered approved
        file raises loudly instead of being silently skipped.
        """

        if not self.approved_dir.exists():
            return None
        loaded = [self._load_approved(p) for p in self.approved_dir.glob("*.json")]
        if not loaded:
            return None
        loaded.sort(key=lambda d: (d.get("approved_at") or 0, d.get("content_hash")))
        return loaded[-1]


__all__ = [
    "OUTCOME_UNCHANGED",
    "OUTCOME_CHANGE",
    "OUTCOME_FAILURE",
    "StoreError",
    "ApprovalIntegrityError",
    "OutcomeRecord",
    "Store",
]
