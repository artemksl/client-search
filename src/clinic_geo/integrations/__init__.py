"""Lazy, opt-in integrations with application-external packages.

Importing this namespace does not import optional external dependencies and
does not alter ClinicGeo's legacy Snapshot, classification, browser,
reporting, or publication workflows.
"""

from __future__ import annotations

from importlib import import_module


__all__ = (
    "ApplicationResult",
    "ResultConsumer",
    "VisibilityMeasurementComponent",
    "adapt_result",
    "coerce_measurement_config",
    "coerce_runtime_config",
    "consume_result",
    "plan_measurement",
    "run_measurement",
    "outreach_archive_payload",
    "store_outreach_archive",
)


def __getattr__(name: str):
    if name not in __all__:
        raise AttributeError(name)
    module_name = (
        "visibility_outreach"
        if name in {"outreach_archive_payload", "store_outreach_archive"}
        else "visibility_runner"
    )
    module = import_module(f"{__name__}.{module_name}")
    return getattr(module, name)


def __dir__() -> list[str]:
    return sorted((*globals(), *__all__))
