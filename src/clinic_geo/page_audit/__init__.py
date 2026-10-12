"""One-page AI audit lane (ADR-002).

Successor to the archived assist v1 (archive/assist-v1-triple-batch/). Stage 3
adds profile/ledger/scheduler/transport; Stage 2 adds manifest/packet/sizing;
Stage 4 adds loader/pipeline/validate/results/service. Modules are owned by
exactly one implementation stage — see docs/implementation/stage*-prompt.md.

Public consumers should import :mod:`clinic_geo.page_audit.api`. The package
exports below remain as a temporary compatibility surface.
"""

from clinic_geo.page_audit.profile import (
    ProfileResolutionError,
    ResolvedProfile,
    StartupRecord,
    build_startup_record,
    pin_model,
    resolve_context_window,
    resolve_profile,
)
from clinic_geo.page_audit.ledger import (
    Ledger,
    LedgerError,
    TERMINAL_STATES,
    default_ledger_path,
)
from clinic_geo.page_audit.scheduler import (
    DispatchOutcome,
    SchedulerSettingsError,
    WaveScheduler,
    WaveSettings,
)
from clinic_geo.page_audit.transport import (
    PageAuditTransport,
    PageDispatchResult,
    TransportError,
    extract_single_json_value,
)

__all__ = [
    "ProfileResolutionError",
    "ResolvedProfile",
    "StartupRecord",
    "build_startup_record",
    "pin_model",
    "resolve_context_window",
    "resolve_profile",
    "Ledger",
    "LedgerError",
    "TERMINAL_STATES",
    "default_ledger_path",
    "DispatchOutcome",
    "SchedulerSettingsError",
    "WaveScheduler",
    "WaveSettings",
    "PageAuditTransport",
    "PageDispatchResult",
    "TransportError",
    "extract_single_json_value",
]
