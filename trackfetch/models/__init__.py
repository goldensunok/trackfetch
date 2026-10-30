"""Typed internal models shared by every part of Trackfetch."""

from trackfetch.models.common import (
    BatchItem,
    Capability,
    ConnectionState,
    ConnectionStatus,
    DuplicatePolicy,
    InputKind,
    ProviderInfo,
    SearchCandidate,
    SearchQuery,
    TransferJob,
    TransferStatus,
    human_bytes,
    human_duration,
    human_speed,
)

__all__ = [
    "BatchItem",
    "Capability",
    "ConnectionState",
    "ConnectionStatus",
    "DuplicatePolicy",
    "InputKind",
    "ProviderInfo",
    "SearchCandidate",
    "SearchQuery",
    "TransferJob",
    "TransferStatus",
    "human_bytes",
    "human_duration",
    "human_speed",
]
