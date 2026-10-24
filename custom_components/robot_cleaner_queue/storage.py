"""Strict atomic storage for command reservations and household cleaning plans.

Home Assistant Store.async_save logs write errors without propagating them. A
robot command must not be dispatched on that ambiguous acknowledgement. Keep the
existing Store envelope and paths, using HA's public atomic JSON helpers instead.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.json import save_json
from homeassistant.util.json import load_json


class DurableStore:
    """Serialized writes whose success means the snapshot reached disk."""

    def __init__(self, hass: HomeAssistant, version: int, key: str) -> None:
        self.hass = hass
        self.version = version
        self.key = key
        self.path = Path(hass.config.path(".storage", key))
        self._lock = asyncio.Lock()
        self._read_failed = False

    async def async_load(self) -> dict | None:
        async with self._lock:
            try:
                envelope = await self.hass.async_add_executor_job(self._load)
                if envelope is not None and (
                    not isinstance(envelope, dict)
                    or type(envelope.get("version")) is not int
                    or envelope["version"] != self.version
                    or envelope.get("minor_version", 1) != 1
                    or envelope.get("key") != self.key
                    or not isinstance(envelope.get("data"), dict)
                ):
                    raise HomeAssistantError("Unsupported robot queue storage. Restore a backup; the file has not been changed.")
            except Exception:
                self._read_failed = True
                raise
            self._read_failed = False
            return envelope["data"] if envelope is not None else None

    def _load(self):
        envelope = load_json(self.path, None)
        if envelope is None and self.path.exists():
            raise HomeAssistantError("Invalid robot queue storage. Restore a backup; the file has not been changed.")
        return envelope

    def _write(self, envelope: dict) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        save_json(str(self.path), envelope, private=True, atomic_writes=True)

    async def async_save(self, data: dict) -> None:
        async with self._lock:
            if self._read_failed:
                raise HomeAssistantError("Robot queue storage could not be read. Restore it before saving; the file has not been changed.")
            envelope = {"version": self.version, "minor_version": 1, "key": self.key, "data": deepcopy(data)}
            # Cancelling an executor await cannot stop its disk write. Keep the
            # write lock until it finishes so a later save cannot be overtaken.
            task = asyncio.ensure_future(self.hass.async_add_executor_job(self._write, envelope))
            cancelled = False
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
            task.result()
            if cancelled:
                raise asyncio.CancelledError
