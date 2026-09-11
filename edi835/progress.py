"""Lightweight in-process progress hook for background MIR conversions."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Callable


_PROGRESS_CALLBACK: ContextVar[Callable[[dict], None] | None] = ContextVar(
    "mir_progress_callback",
    default=None,
)


def set_progress_callback(callback: Callable[[dict], None] | None):
    return _PROGRESS_CALLBACK.set(callback)


def reset_progress_callback(token) -> None:
    _PROGRESS_CALLBACK.reset(token)


def report_progress(**payload) -> None:
    callback = _PROGRESS_CALLBACK.get()
    if callback is None:
        return
    try:
        callback(payload)
    except Exception:
        # Progress reporting must never fail the conversion itself.
        pass
