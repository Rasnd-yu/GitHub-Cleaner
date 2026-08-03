"""
GitHub repository collector package
"""

from . import trending_2022
from . import trending_2024
from . import trending_2025
from . import collections
from . import topics
from . import accompanying
from . import ossf_criticality
from . import ossf_scorecard
from . import core_developers
from . import core_developers_enricher

__all__ = [
    'trending_2024',
    'trending_2025',
    'collections',
    'topics',
    'accompanying',
    'ossf_criticality',
    'ossf_scorecard',
    'core_developers',
    'core_developers_enricher'
]