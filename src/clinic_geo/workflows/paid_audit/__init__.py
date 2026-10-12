"""Paid Diagnostic Audit integration.

The canonical public surface is :mod:`clinic_geo.workflows.paid_audit.api`.
Package-level imports remain as a compatibility surface for existing Python
callers; new code should import the explicit facade.
"""

from .api import *  # noqa: F401,F403
from .api import __all__
