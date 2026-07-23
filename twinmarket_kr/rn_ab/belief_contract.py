"""Immutable six-dimension limits for the approved RN baseline.

The limits are carried in a sealed ``StudySpec`` so they become part of the
run manifest.  They are nevertheless not a free tuning knob within this
baseline: the 0720 Samsung prompt contract fixes dim_1 at 150 Korean
characters and dim_2 through dim_6 at 100.  Keeping the canonical map here
lets the authoring, runtime-adapter, and persistence boundaries reject the
same accidental drift.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Final, Mapping


RN_BASELINE_BELIEF_LIMITS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "dim_1": 150,
        "dim_2": 100,
        "dim_3": 100,
        "dim_4": 100,
        "dim_5": 100,
        "dim_6": 100,
    }
)


def has_rn_baseline_belief_limits(value: Mapping[str, int]) -> bool:
    """Return whether an already-normalized map is the approved baseline map."""

    return dict(value) == dict(RN_BASELINE_BELIEF_LIMITS)
