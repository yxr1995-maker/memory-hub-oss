"""Non-sensitive, in-process service metadata for health probes.

This module intentionally does not import the storage/service layers or inspect
any user data. Build metadata is supplied by the launcher when available.
"""
from __future__ import annotations

import os
import time

_PROCESS_STARTED_AT = time.time()
_CAPABILITIES = ("recall_for_decision", "read_evidence", "record_episode", "service_health")


def health_metadata() -> dict:
    """Return only static service identity, capabilities, and process metadata."""
    return {
        "version": os.environ.get("MEMORY_HUB_VERSION", "unknown"),
        "build": os.environ.get("MEMORY_HUB_BUILD_ID", "unknown"),
        "capabilities": list(_CAPABILITIES),
        "process": {"pid": os.getpid(), "started_at": _PROCESS_STARTED_AT},
    }
