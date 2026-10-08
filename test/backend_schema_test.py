"""The service schema is the card's contract: verify the payloads it sends.

Offline and robot-free. Voluptuous is the schema engine Home Assistant uses; the
suite skips cleanly when it is not installed, and CI installs it.
"""
import ast
import types
import unittest
from pathlib import Path

try:
    import voluptuous as vol
except ImportError:  # pragma: no cover - CI installs it
    vol = None

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "custom_components/robot_cleaner_queue/__init__.py"


def service_schema():
    tree = ast.parse(SOURCE.read_text())
    wanted = {"ROOM_SCHEMA", "PLAN_ROOMS", "SERVICE_SCHEMA"}
    body = [node for node in tree.body
            if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in wanted]

    def ensure_list(value):
        if value is None:
            return []
        return value if isinstance(value, list) else [value]

    env = {"vol": vol, "cv": types.SimpleNamespace(ensure_list=ensure_list)}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SOURCE), "exec"), env)  # noqa: S102
    return env["SERVICE_SCHEMA"]


@unittest.skipIf(vol is None, "voluptuous is not installed")
class ServiceSchemaTests(unittest.TestCase):
    """What the card sends must validate; what it must never send must not."""

    def setUp(self):
        self.schema = service_schema()

    def accepts(self, payload):
        try:
            self.schema(payload)
            return True
        except vol.Invalid:
            return False

    def test_room_plans_validate(self):
        for payload in (
            {"command": "start_manual", "vacuum": "vacuum.robot",
             "rooms": [{"id": "0_12", "mode": "vacuum_mop", "suction": "max", "water": "high", "route": "standard"},
                       {"id": "0_13", "mode": "mop", "water": "low", "route": "deep", "repeat": 2}]},
            {"command": "start_manual", "vacuum": "vacuum.robot", "rooms": [{"id": "0_1", "mode": "mop"}]},
            {"command": "start_manual", "vacuum": "vacuum.robot", "setup": {"mode": "vacuum", "repeat": 1},
             "rooms": [{"id": "0_1"}, {"id": "0_2", "mode": "mop"}]},
            {"command": "toggle_saved", "vacuum": "vacuum.robot"},
        ):
            self.assertTrue(self.accepts(payload), payload)

    def test_legacy_area_plans_still_validate(self):
        self.assertTrue(self.accepts({"command": "start_manual", "vacuum": "vacuum.robot",
                                      "rooms": ["kitchen"], "setup": {"mode": "vacuum"}}))

    def test_malformed_and_removed_payloads_are_refused(self):
        for payload in (
            {"command": "start_manual", "rooms": [{"mode": "mop"}]},          # a room needs an id
            {"command": "start_manual", "rooms": [{"id": "0_1", "repeat": 9}]},
            {"command": "start_manual", "rooms": [{"id": "0_1", "mode": "teleport"}]},
            {"command": "start", "presets": ["button.robot_kitchen"]},        # routines were removed
        ):
            self.assertFalse(self.accepts(payload), payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
