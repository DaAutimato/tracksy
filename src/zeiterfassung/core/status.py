"""Statusobjekt (JSON-fähig) für UI, Tray, CLI und D-Bus.

Format: siehe ``build_status`` unten.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .models import Entry, EntryStatus, StartRequest, State
from .timeutil import to_iso

STATUS_VERSION = 1

__all__ = ["STATUS_VERSION", "state_of", "build_status", "status_to_json"]


def state_of(entry: Entry | None) -> State:
    if entry is None or not entry.is_active:
        return State.IDLE
    return {
        EntryStatus.RUNNING: State.RUNNING,
        EntryStatus.PAUSED: State.PAUSED,
        EntryStatus.STOPPING: State.STOPPING,
    }[EntryStatus(entry.status)]


def build_status(
    entry: Entry | None,
    now: datetime,
    pending_start: StartRequest | None = None,
    last_stopped: Entry | None = None,
    recovered: bool = False,
) -> dict[str, Any]:
    state = state_of(entry)
    active = entry if state is not State.IDLE else None
    last = None
    if last_stopped is not None:
        last = {
            "uuid": last_stopped.uuid,
            "customer": last_stopped.customer,
            "topic": last_stopped.topic,
            "ticket": last_stopped.ticket,
            "location": str(last_stopped.location),
            "end_at": to_iso(last_stopped.end_at),
        }
    return {
        "version": STATUS_VERSION,
        "state": str(state),
        "generated_at": to_iso(now),
        "entry": active.to_dict(now) if active is not None else None,
        "pending_start": pending_start.to_dict() if pending_start is not None else None,
        "last_stopped": last,
        "recovered": bool(recovered and active is not None),
    }


def status_to_json(status: dict[str, Any]) -> str:
    return json.dumps(status, ensure_ascii=False, sort_keys=True)
