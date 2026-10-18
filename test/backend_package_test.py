"""Validate the shipped wrapper after actual Home Assistant package merging.

Run with Home Assistant installed, not the backend's mock-module harness. The only
stub is integration discovery: the real script component/config module determines
merge behavior. No robot services are registered or called.
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
import homeassistant.components.script as script_component
from homeassistant.components.script.config import SCRIPT_ENTITY_SCHEMA
from homeassistant.components.script import config as script_config
from homeassistant.helpers.template import Template
from homeassistant.util.yaml import load_yaml


PACKAGE = (Path(sys.argv.pop(1)).resolve() if len(sys.argv) > 1 and not sys.argv[1].startswith("-")
           else Path(__file__).resolve().parents[1] / "packages" / "robot_cleaner_queue.yaml")
EXPECTED_SELECTORS = {
    "command": "select", "vacuum": "entity", "run_id": "text", "current_index": "number",
    "water_refilled": "boolean", "recovery_token": "text", "rooms": "object", "setup": "object",
}


class PackageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        self.package = load_yaml(str(PACKAGE))

    async def asyncTearDown(self):
        await self.hass.async_stop()
        self.temp.cleanup()

    async def merged_wrapper(self, package):
        # Exercise HA's real package entry point, including recursive merging.
        # The script subtree is the entire shipped wrapper, without filtering or
        # normalizing selectors before merging. Other domains cannot affect it.
        integration = SimpleNamespace(
            async_get_component=AsyncMock(return_value=script_component),
            async_get_platform=AsyncMock(return_value=script_config),
        )
        errors = []
        with patch("homeassistant.config.async_get_integration_with_requirements", AsyncMock(return_value=integration)):
            merged = await merge_packages_config(
                self.hass, {}, {"robot_queue": {"script": copy.deepcopy(package["script"])}},
                lambda *args: errors.append(args[-1]),
            )
        self.assertEqual(errors, [])
        return merged["script"]["robot_cleaner_queue_control"]

    async def test_empty_mapping_selectors_reproduce_the_package_failure(self):
        # A raw YAML/schema-only check accepts {}, but merging deletes the key.
        for field, selector in [("run_id", "text"), ("water_refilled", "boolean")]:
            with self.subTest(field=field):
                broken = copy.deepcopy(self.package)
                broken["script"]["robot_cleaner_queue_control"]["fields"][field]["selector"] = {selector: {}}
                SCRIPT_ENTITY_SCHEMA(copy.deepcopy(broken["script"]["robot_cleaner_queue_control"]))
                wrapper = await self.merged_wrapper(broken)
                self.assertEqual(wrapper["fields"][field]["selector"], {})
                with self.assertRaisesRegex(Exception, "Only one type can be specified"):
                    SCRIPT_ENTITY_SCHEMA(wrapper)

    async def test_shipped_package_preserves_all_selectors_and_validates(self):
        wrapper = await self.merged_wrapper(self.package)
        self.assertEqual(set(wrapper["fields"]), set(EXPECTED_SELECTORS))
        for field, selector in EXPECTED_SELECTORS.items():
            self.assertEqual(set(wrapper["fields"][field]["selector"]), {selector})
        validated = SCRIPT_ENTITY_SCHEMA(wrapper)
        self.assertEqual(validated["mode"], "parallel")
        self.assertEqual(validated["max"], 10)
        self.assertEqual(len(validated["sequence"]), 1)
        self.assertEqual(validated["fields"]["current_index"]["selector"]["number"]["min"], 0)
        options = validated["fields"]["command"]["selector"]["select"]["options"]
        self.assertIn("continue_sequence", options)
        self.assertIn("resume_now", options)

    async def test_continuation_templates_preserve_run_pass_and_acknowledgement(self):
        validated = SCRIPT_ENTITY_SCHEMA(await self.merged_wrapper(self.package))
        branch = validated["sequence"][0]["choose"][1]
        data = branch["sequence"][0]["data"]
        for command in ("continue_sequence", "resume_now"):
            for acknowledgement in (None, False, True):
                with self.subTest(command=command, acknowledgement=acknowledgement):
                    variables = {"command": command, "vacuum": "vacuum.robot", "run_id": "retained-run", "current_index": 3, "recovery_token": "native-job-fingerprint"}
                    if acknowledgement is not None:
                        variables["water_refilled"] = acknowledgement
                    condition = branch["conditions"][0]["value_template"]
                    self.assertIsInstance(condition, Template)
                    self.assertTrue(condition.async_render(variables))
                    self.assertTrue(all(isinstance(value, Template) for value in data.values()))
                    rendered = {key: value.async_render(variables) for key, value in data.items()}
                    self.assertEqual(rendered, {**variables, "water_refilled": bool(acknowledgement)})
                    self.assertIs(type(rendered["current_index"]), int)
                    self.assertIs(type(rendered["water_refilled"]), bool)


if __name__ == "__main__":
    print("Real Home Assistant package regression:", const.__version__, PACKAGE)
    unittest.main()
