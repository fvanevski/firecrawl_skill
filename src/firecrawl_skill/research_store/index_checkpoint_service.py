"""Durable PostgreSQL indexing checkpoints and guarded finalization."""

from .index_checkpoint_asset_membership import (
    _IndexCheckpointAssetMembershipMixin,
)
from .index_checkpoint_core import _IndexCheckpointCoreMixin
from .index_checkpoint_finalize import _IndexCheckpointFinalizeMixin
from .index_checkpoint_store import IndexCheckpointStoreMixin


class IndexCheckpointService(
    _IndexCheckpointCoreMixin,
    _IndexCheckpointFinalizeMixin,
    _IndexCheckpointAssetMembershipMixin,
    IndexCheckpointStoreMixin,
):
    """Seal, observe, resume, and atomically finalize one run's index set."""
