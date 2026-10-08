"""Where the dashboard's data comes from.

simulated (default)  fake Phase 1/2 output in the exact same format; no keys or merged code needed
live                 the real Phase 1 retriever and Phase 2 LangGraph pipeline

Pick one with the sidebar, or set COPILOT_BACKEND=simulated|live before starting.
"""

import os

from .base import BackendUnavailable, ComplianceBackend, PolicyDocument

MODES = ("simulated", "live")


def default_mode() -> str:
    mode = os.getenv("COPILOT_BACKEND", "simulated").strip().lower()
    return mode if mode in MODES else "simulated"


def create_backend(mode: str, **options) -> ComplianceBackend:
    if mode == "simulated":
        from .simulated import SimulatedBackend

        return SimulatedBackend(**options)
    if mode == "live":
        from .live import LiveBackend

        return LiveBackend(**options)
    raise ValueError(f"Unknown backend mode: {mode!r}")


__all__ = ["MODES", "BackendUnavailable", "ComplianceBackend", "PolicyDocument", "create_backend", "default_mode"]
