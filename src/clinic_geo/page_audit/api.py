"""Public facade for the one-page AI audit lane.

Consumers outside :mod:`clinic_geo.page_audit` should import this module rather
than depending on the lane's implementation modules.
"""

from clinic_geo.page_audit.service import (
    PageAuditResultsError,
    PageAuditResultsNotReadyError,
    PageAuditService,
)
from clinic_geo.page_audit.profile import (
    ResolvedProfile,
    StartupRecord,
    build_startup_record,
    resolve_profile,
)
from clinic_geo.page_audit.ledger import (
    Ledger,
    LedgerError,
    TERMINAL_STATES,
    default_ledger_path,
)
from clinic_geo.page_audit.scheduler import WaveScheduler
from clinic_geo.page_audit.transport import PageAuditTransport


__all__ = [
    "PageAuditService",
    "PageAuditResultsError",
    "PageAuditResultsNotReadyError",
    "resolve_profile",
    "ResolvedProfile",
    "StartupRecord",
    "build_startup_record",
    "Ledger",
    "LedgerError",
    "TERMINAL_STATES",
    "default_ledger_path",
    "WaveScheduler",
    "PageAuditTransport",
]
