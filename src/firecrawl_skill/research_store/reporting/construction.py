"""Report-construction boundary.

``report_service.py`` has reviewed path-keyed Pyrefly debt.  Keep the current
implementation path stable in #264 and expose it through the canonical
reporting namespace; moving that debt without fixing it would invalidate the
repository baseline contract.
"""

from ..report_service import LocalSynthesisService

__all__ = ["LocalSynthesisService"]
