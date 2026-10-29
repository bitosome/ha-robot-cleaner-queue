"""Validate the integration-only package using real Home Assistant package merging.

No services are invoked. The shipped package must not add a second control path
through scripts, automations or input helpers.
"""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant import const
from homeassistant.config import merge_packages_config
from homeassistant.core import HomeAssistant
from homeassistant.util.yaml import load_yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from custom_components import robot_cleaner_queue as component

PACKAGE = (Path(sys.argv.pop(1)).resolve() if len(sys.argv) > 1 and not sys.argv[1].startswith("-")
           else ROOT / "packages" / "robot_cleaner_queue.yaml")


class PackageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        self.package = load_yaml(str(PACKAGE))

    async def asyncTearDown(self):
        await self.hass.async_stop()
        self.temp.cleanup()

    async def merged_package(self, initial):
        integration = SimpleNamespace(
            async_get_component=AsyncMock(return_value=component),
            async_get_platform=AsyncMock(side_effect=ImportError),
        )
        errors = []
        with patch("homeassistant.config.async_get_integration_with_requirements", AsyncMock(return_value=integration)):
            merged = await merge_packages_config(
                self.hass, copy.deepcopy(initial), {"robot_queue": copy.deepcopy(self.package)},
                lambda *args: errors.append(args[-1]),
            )
        self.assertEqual(errors, [])
        component.CONFIG_SCHEMA(merged)
        return merged

    async def test_package_only_enables_integration(self):
        self.assertEqual(self.package, {"robot_cleaner_queue": {}})
        self.assertEqual(await self.merged_package({}), {"robot_cleaner_queue": {}})

    async def test_merge_preserves_unrelated_configuration_without_wrappers(self):
        original = {"script": {"unrelated": {"sequence": []}}, "input_boolean": {"unrelated": {}}}
        merged = await self.merged_package(original)
        self.assertEqual(merged, {**original, "robot_cleaner_queue": {}})
        self.assertNotIn("robot_cleaner_queue_control", merged["script"])
        self.assertNotIn("automation", merged)


if __name__ == "__main__":
    print("Real Home Assistant package regression:", const.__version__, PACKAGE)
    unittest.main()
