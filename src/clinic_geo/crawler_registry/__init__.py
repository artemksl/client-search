"""Weekly live-facts crawler registry (ADR-002 §6).

A weekly verification pipeline for deterministic audit checks that depend on
changing external crawler facts. One model call per week proposes a structured
registry (:mod:`propose`); deterministic code validates, re-fetches, checks
literal evidence, diffs and computes ``isChange`` (:mod:`verify`); a human
approves (:mod:`store`); audits pin an immutable approved version by content hash.

The model NEVER decides whether the registry changed, a failed check is NEVER
read as "no change", and a candidate is NEVER auto-promoted. The submodules are
imported lazily by callers: :mod:`schema`, :mod:`verify` and :mod:`store` are
stdlib/HTTP-only, while :mod:`propose` and :mod:`cli` pull in the shared
page-audit queue.
"""

__all__ = ["schema", "verify", "store", "propose", "cli"]
