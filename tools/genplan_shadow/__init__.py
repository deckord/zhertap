"""Shadow-only comparison of land candidates with reviewed genplan layers."""

from .engine import compare_candidate
from .models import ComparisonRequest, ComparisonResult, Decision

__all__ = [
    "ComparisonRequest",
    "ComparisonResult",
    "Decision",
    "compare_candidate",
]
