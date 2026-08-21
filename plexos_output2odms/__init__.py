"""PLEXOS optimized dispatch to PSS ODMS SSH adapter."""

from .pipeline import SnapshotConfig, SnapshotResult, build_dispatch_snapshot

__all__ = ["SnapshotConfig", "SnapshotResult", "build_dispatch_snapshot"]
__version__ = "0.2.0"
