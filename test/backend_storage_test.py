"""Actual HA atomic-storage boundaries, with only robot traffic mocked."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store as NativeStore
from homeassistant.util.file import WriteError

import backend_manual_test as manual
from backend_queue_test import load

DurableStore = load("storage").DurableStore


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        self.storage = DurableStore(self.hass, 1, "robot_cleaner_queue")

    async def asyncTearDown(self):
        await self.hass.async_stop()
        self.temp.cleanup()

    async def test_existing_native_store_loads_without_migration_and_writes_private_atomic_json(self):
        original = {"run_id": "saved-run", "rooms": ["0_1", "0_2"]}
        await NativeStore(self.hass, 1, self.storage.key).async_save(original)
        before = self.storage.path.read_bytes()
        self.assertEqual(await self.storage.async_load(), original)
        self.assertEqual(self.storage.path.read_bytes(), before)
        updated = {**original, "completed": 1}
        await self.storage.async_save(updated)
        self.assertEqual(await DurableStore(self.hass, 1, self.storage.key).async_load(), updated)
        self.assertEqual(self.storage.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.storage.path.read_text()), {
            "version": 1, "minor_version": 1, "key": self.storage.key, "data": updated,
        })

    async def test_real_atomic_write_failure_propagates_and_preserves_previous_file(self):
        await self.storage.async_save({"revision": 1})
        before = self.storage.path.read_bytes()
        with patch("homeassistant.helpers.json.write_utf8_file_atomic", side_effect=WriteError("synthetic disk full")):
            with self.assertRaisesRegex(HomeAssistantError, "disk full"):
                await self.storage.async_save({"revision": 2})
        self.assertEqual(self.storage.path.read_bytes(), before)
        await self.storage.async_save({"revision": 2})
        self.assertEqual(await self.storage.async_load(), {"revision": 2})

    async def test_unreadable_or_incompatible_storage_cannot_be_replaced_by_empty_queue(self):
        self.storage.path.parent.mkdir()
        for invalid in ["broken JSON", json.dumps({"version": 2, "key": self.storage.key, "data": {}}),
                        json.dumps({"version": 1, "key": "another-store", "data": {}})]:
            self.storage.path.write_text(invalid)
            with self.assertRaises(HomeAssistantError):
                await self.storage.async_load()
            with self.assertRaisesRegex(HomeAssistantError, "could not be read"):
                await self.storage.async_save({})
            self.assertEqual(self.storage.path.read_text(), invalid)

    async def test_cancelled_executor_write_finishes_before_a_following_save(self):
        started, release = threading.Event(), threading.Event()
        original = self.storage._write
        order = []
        def slow_write(envelope):
            if envelope["data"]["revision"] == 1:
                started.set()
                if not release.wait(5):
                    raise TimeoutError("test release timeout")
            original(envelope)
            order.append(envelope["data"]["revision"])
        self.storage._write = slow_write
        first = asyncio.create_task(self.storage.async_save({"revision": 1}))
        second = None
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 5))
            first.cancel()
            await asyncio.sleep(0)
            first.cancel()
            second = asyncio.create_task(self.storage.async_save({"revision": 2}))
            await asyncio.sleep(0)
            self.assertFalse(first.done())
            self.assertFalse(second.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await first
        await second
        self.assertEqual(order, [1, 2])
        self.assertEqual(await self.storage.async_load(), {"revision": 2})

    async def test_manager_never_dispatches_when_actual_command_reservation_cannot_be_written(self):
        fixture = manual.ManagerTraceTests(methodName="runTest")
        await fixture.asyncSetUp()
        fixture.manager.store = self.storage
        await self.storage.async_save(fixture.manager.queue.dump())
        before = self.storage.path.read_bytes()
        with patch("homeassistant.helpers.json.write_utf8_file_atomic", side_effect=WriteError("synthetic disk full")):
            await fixture.start()
        self.assertEqual(fixture.calls, [])
        self.assertEqual(fixture.segment_calls, [])
        self.assertEqual(fixture.manager.queue.phase, "attention")
        self.assertEqual(self.storage.path.read_bytes(), before)

    async def test_saved_plan_failure_never_publishes_success_or_replaces_shared_plan(self):
        fixture = manual.ManagerTraceTests(methodName="runTest")
        await fixture.asyncSetUp()
        fixture.manager.preset_store = self.storage
        await fixture.save_manual()
        before = self.storage.path.read_bytes()
        saved = fixture.manager.saved_presets
        with patch("homeassistant.helpers.json.write_utf8_file_atomic", side_effect=WriteError("synthetic disk full")):
            with self.assertRaises(HomeAssistantError):
                await fixture.save_manual(rooms=["office"])
        self.assertIs(fixture.manager.saved_presets, saved)
        self.assertEqual(self.storage.path.read_bytes(), before)
        self.assertEqual(fixture.calls, [])

    async def test_cancelled_shared_save_publishes_durable_revision_before_unlocking(self):
        fixture = manual.ManagerTraceTests(methodName="runTest")
        await fixture.asyncSetUp()
        fixture.manager.preset_store = self.storage
        started, release = threading.Event(), threading.Event()
        original = self.storage._write
        def slow_write(envelope):
            started.set()
            if not release.wait(5):
                raise TimeoutError("test release timeout")
            original(envelope)
        self.storage._write = slow_write
        request = asyncio.create_task(fixture.save_manual())
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 5))
            request.cancel()
            await asyncio.sleep(0)
            request.cancel()
            self.assertTrue(fixture.manager.lock.locked())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await request
        self.assertEqual(fixture.manager.saved_presets, await self.storage.async_load())
        self.assertEqual(fixture.manager.saved_presets["vacuum.robot"]["revision"], 1)
        self.assertEqual(fixture.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
