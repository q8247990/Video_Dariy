"""Application-layer use cases for Home Profile endpoints.

Currently exposes the visual-appearance generation use case (see
:mod:`src.application.home_profile.use_case_vision`). Future Home
Profile endpoints with infrastructure dependencies should be added here.
"""

from src.application.home_profile.use_case_vision import (
    GenerateEntityAppearanceResult,
    generate_entity_appearance_use_case,
)

__all__ = [
    "GenerateEntityAppearanceResult",
    "generate_entity_appearance_use_case",
]
