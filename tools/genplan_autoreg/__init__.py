"""Conservative raster general-plan auto-registration proposals."""

from .models import (
    AutoregResult,
    BoundingBox,
    DiagnosticAnchorPoint,
    MatchMetrics,
    ProposedGCP,
)
from .pipeline import AutoregConfig, run_autoregistration

__all__ = [
    "AutoregConfig",
    "AutoregResult",
    "BoundingBox",
    "DiagnosticAnchorPoint",
    "MatchMetrics",
    "ProposedGCP",
    "run_autoregistration",
]
