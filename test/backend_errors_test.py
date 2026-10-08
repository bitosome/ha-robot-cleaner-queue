"""Offline coverage for diagnostic-only, payload-free exception summaries."""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "custom_components/robot_cleaner_queue/errors.py"
spec = importlib.util.spec_from_file_location("queue_errors", SOURCE)
errors = importlib.util.module_from_spec(spec)
spec.loader.exec_module(errors)


def native(name="RoborockException"):
    return type(name, (Exception,), {"__module__": "roborock.exceptions"})


def wrapped(cause, **attributes):
    cls = type("HomeAssistantError", (Exception,), {"__module__": "homeassistant.exceptions"})
    error = cls("SECRET credentials")
    error.translation_domain = "roborock"
    error.translation_key = "command_failed"
    error.translation_placeholders = {"command": "SECRET payload"}
    error.__dict__.update(attributes)
    error.__cause__ = cause
    return error


class ErrorMetadataTests(unittest.TestCase):
    def test_ha_wrapped_native_timeout_keeps_cause_and_no_payload(self):
        timeout = TimeoutError("SECRET server URL")
        command = native()("SECRET native payload")
        command.__cause__ = timeout
        result = errors.command_failure_metadata(wrapped(command), "manual")
        self.assertEqual(result["category"], "timeout")
        self.assertEqual(result["exception_types"], ["HomeAssistantError", "RoborockException", "TimeoutError"])
        self.assertEqual(result["translation_keys"], ["command_failed"])
        self.assertNotIn("SECRET", json.dumps(result))

    def test_actual_v1_error_dictionary_retains_only_bounded_numeric_code(self):
        error = native("RoborockInvalidStatus")({"code": -10007, "message": "SECRET credentials", "payload": {"token": "SECRET"}})
        result = errors.command_failure_metadata(wrapped(error), "manual")
        self.assertEqual(result["category"], "rejected")
        self.assertEqual(result["codes"], [-10007])
        self.assertNotIn("SECRET", json.dumps(result))

    def test_numeric_attributes_and_invalid_values(self):
        for value in ("-10007 SECRET", True, 2**100, {"code": -10007}, [-10007], None):
            error = native()({"code": value, "message": "SECRET"})
            error.code = error.error_code = error.errno = value
            self.assertEqual(errors.command_failure_metadata(error, "manual")["codes"], [])
        error = native()({"code": -10007})
        error.error_code, error.code, error.errno = -10007, 12, -1
        self.assertEqual(errors.command_failure_metadata(error, "manual")["codes"], [12, -10007, -1])

    def test_connection_error_preserves_numeric_errno_without_filename(self):
        error = ConnectionRefusedError(61, "SECRET server", "SECRET filename")
        result = errors.command_failure_metadata(wrapped(error), "manual")
        self.assertEqual(result["category"], "connection")
        self.assertEqual(result["codes"], [61])
        self.assertNotIn("SECRET", json.dumps(result))

    def test_message_like_args_and_unrecognized_attributes_are_ignored(self):
        error = native()({"error": {"code": -10007}, "message": "SECRET"})
        error.status_code = 500
        error.payload = {"code": -1, "token": "SECRET"}
        result = errors.command_failure_metadata(error, "device")
        self.assertEqual(result["codes"], [])
        self.assertNotIn("SECRET", json.dumps(result))

    def test_nested_context_and_cycles_terminate(self):
        first = wrapped(native("RoborockConnectionException")("SECRET"))
        first.__context__ = TimeoutError("SECRET")
        first.__context__.__cause__ = first
        first.__cause__.__context__ = first
        result = errors.command_failure_metadata(first, "start")
        self.assertEqual(result["category"], "timeout")
        self.assertEqual(len(result["exception_types"]), 3)
        self.assertNotIn("SECRET", json.dumps(result))

    def test_type_and_translation_keys_are_allowlisted(self):
        secret_type = type("SECRET_token", (Exception,), {"__module__": "SECRET_module"})
        result = errors.command_failure_metadata(wrapped(secret_type("SECRET"), translation_key="SECRET_key"), "SECRET_operation")
        self.assertEqual(result["operation"], "unknown")
        self.assertEqual(result["exception_types"], ["HomeAssistantError", "Exception"])
        self.assertEqual(result["translation_keys"], [])
        self.assertNotIn("SECRET", json.dumps(result))
        other_domain = wrapped(ValueError(), translation_domain="SECRET_domain")
        self.assertEqual(errors.command_failure_metadata(other_domain, "configure")["translation_keys"], [])

    def test_never_calls_str_repr_or_custom_property(self):
        class Hostile(Exception):
            def __str__(self): raise AssertionError("Rendered exception")
            def __repr__(self): raise AssertionError("Rendered exception")
            @property
            def code(self): raise AssertionError("Evaluated arbitrary property")
        result = errors.command_failure_metadata(Hostile("SECRET"), "manual")
        self.assertEqual(result["category"], "unknown")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_exception_groups_and_deep_chains_are_bounded(self):
        root = wrapped(ExceptionGroup("SECRET", [native()({"code": value}) for value in range(100)]))
        result = errors.command_failure_metadata(root, "configure")
        self.assertLessEqual(len(result["codes"]), errors.MAX_EXCEPTION_NODES)
        self.assertNotIn("SECRET", json.dumps(result))
        chain = wrapped(None)
        node = chain
        for _ in range(100):
            node.__cause__ = wrapped(None)
            node = node.__cause__
        node.__cause__ = TimeoutError()
        result = errors.command_failure_metadata(chain, "manual")
        self.assertEqual(result["category"], "unknown", "Traversal must stop at its hard node limit")

    def test_categories_are_diagnostic_and_unknown_remains_unknown(self):
        for error, expected in ((ConnectionResetError(), "connection"), (native("RoborockTimeout")(), "timeout"),
                                (native("RoborockUnsupportedFeature")(), "rejected"),
                                (ValueError(), "rejected"), (native()(), "unknown"), (RuntimeError(), "unknown")):
            result = errors.command_failure_metadata(error, "manual")
            self.assertEqual(result["category"], expected)
            self.assertNotIn("definitely_not_sent", result)
            self.assertNotIn("retry", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
