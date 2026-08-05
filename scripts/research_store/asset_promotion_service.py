"""PostgreSQL-authoritative asset promotion and completion membership."""

from __future__ import annotations

from .asset_promotion_core import _AssetPromotionCoreMixin
from .asset_promotion_models import (
    AssetMembershipMember,
    AssetMembershipSeal,
    AssetMembershipSealedError,
    AssetPromotionCompatibilityError,
    AssetPromotionError,
    AssetPromotionPending,
)
from .asset_promotion_seal import _AssetPromotionSealMixin
from .asset_promotion_store import _AssetPromotionStoreMixin


class AssetPromotionService(
    _AssetPromotionCoreMixin,
    _AssetPromotionSealMixin,
    _AssetPromotionStoreMixin,
):
    """Advance explicit stages and seal the exact completion-critical set."""

    DEFAULT_POLICY_VERSION = "completion-membership-v1"

    def __init__(self, uow_factory):
        self.uow_factory = uow_factory


__all__ = [
    "AssetMembershipMember",
    "AssetMembershipSeal",
    "AssetMembershipSealedError",
    "AssetPromotionCompatibilityError",
    "AssetPromotionError",
    "AssetPromotionPending",
    "AssetPromotionService",
]
