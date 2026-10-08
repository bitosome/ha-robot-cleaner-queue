"""Small, allowlisted command-failure diagnostics without native error payloads.

These categories describe the exception, never whether a motion command reached
its destination. Native transports can retry internally or fail during readback
following a successful send. The caller must track the dispatch boundary itself.
"""
from __future__ import annotations

MAX_EXCEPTION_NODES = 16
MAX_CODE = 2**31 - 1
OPERATIONS = frozenset({"configure", "manual", "start", "resume", "pause", "stop",
                        "return_to_dock", "device"})
TRANSLATION_DOMAINS = frozenset({"roborock", "homeassistant", "vacuum", "select"})
TRANSLATION_KEYS = frozenset({"command_failed", "update_options_failed", "option_not_valid",
                             "not_supported", "not_available", "entity_not_available",
                             "service_not_found", "no_entity_specified", "invalid_entity_format"})

# Use known module/class pairs. Arbitrary subclass names can contain data too;
# unknown names are represented by the first recognized base or UnknownError.
KNOWN_TYPES = {
    "builtins": {
        "Exception": "unknown", "BaseException": "unknown",
        "ExceptionGroup": "unknown", "BaseExceptionGroup": "unknown",
        "TimeoutError": "timeout", "ConnectionError": "connection",
        "ConnectionAbortedError": "connection", "ConnectionRefusedError": "connection",
        "ConnectionResetError": "connection", "BrokenPipeError": "connection",
        "OSError": "unknown", "ValueError": "rejected", "TypeError": "rejected",
        "PermissionError": "rejected", "RuntimeError": "unknown",
    },
    "roborock.exceptions": {
        "RoborockException": "unknown", "RoborockTimeout": "timeout",
        "RoborockConnectionException": "connection", "RoborockBackoffException": "connection",
        "RoborockInvalidStatus": "rejected", "RoborockDeviceBusy": "rejected",
        "RoborockUnsupportedFeature": "rejected", "RoborockMissingParameters": "rejected",
        "UnknownMethodError": "rejected", "RoborockTooManyRequest": "rejected",
        "RoborockRateLimit": "rejected", "RoborockParsingException": "unknown",
        "VacuumError": "rejected", "CommandVacuumError": "rejected",
    },
    "homeassistant.exceptions": {
        "HomeAssistantError": "unknown", "ServiceValidationError": "rejected",
        "Unauthorized": "rejected", "InvalidEntityFormatError": "rejected",
        "NoEntitySpecifiedError": "rejected", "ServiceNotFound": "rejected",
        "ConfigValidationError": "rejected",
    },
    "aiohttp.client_exceptions": {
        "ClientConnectionError": "connection", "ClientConnectorError": "connection",
        "ClientOSError": "connection", "ServerConnectionError": "connection",
        "ServerDisconnectedError": "connection", "ServerTimeoutError": "timeout",
        "ConnectionTimeoutError": "timeout", "SocketTimeoutError": "timeout",
    },
}


def _known_type(error: BaseException) -> tuple[str, str, str]:
    for cls in type(error).__mro__:
        module, name = cls.__module__, cls.__name__
        if category := KNOWN_TYPES.get(module, {}).get(name):
            return module, name, category
    return "", "UnknownError", "unknown"


def _numeric_code(value: object) -> bool:
    return type(value) is int and -MAX_CODE - 1 <= value <= MAX_CODE


def command_failure_metadata(error: BaseException, operation: str) -> dict:
    """Return bounded metadata; do not render messages, args, payloads or traces.

    Both cause and context are inspected because HA and transport wrappers use
    both. Cycles and exception groups are bounded. Categories are diagnostic only:
    even a rejection can follow an accepted command on another transport.
    """
    result = {"operation": operation if operation in OPERATIONS else "unknown",
              "category": "unknown", "exception_types": [], "codes": [],
              "translation_keys": []}
    pending = [error]
    seen: set[int] = set()
    categories: set[str] = set()
    while pending and len(seen) < MAX_EXCEPTION_NODES:
        current = pending.pop(0)
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        module, name, category = _known_type(current)
        categories.add(category)
        if name not in result["exception_types"]:
            result["exception_types"].append(name)

        # Access instance data without evaluating a custom exception's properties.
        attributes = BaseException.__dict__["__dict__"].__get__(current)
        if module in {"roborock.exceptions", "aiohttp.client_exceptions", "builtins"}:
            for key in ("code", "error_code", "errno"):
                value = attributes.get(key)
                if _numeric_code(value) and value not in result["codes"]:
                    result["codes"].append(value)
        if isinstance(current, OSError):
            # OSError stores errno in a native descriptor, outside __dict__.
            value = OSError.__dict__["errno"].__get__(current)
            if _numeric_code(value) and value not in result["codes"]:
                result["codes"].append(value)
        if module == "roborock.exceptions":
            # python-roborock V1 puts the API error dict in args[0], not a code
            # attribute. Read this one numeric field; never retain the dict.
            args = BaseException.__dict__["args"].__get__(current)
            if args and type(args[0]) is dict:
                value = args[0].get("code")
                if _numeric_code(value) and value not in result["codes"]:
                    result["codes"].append(value)
        if module == "homeassistant.exceptions":
            domain, key = attributes.get("translation_domain"), attributes.get("translation_key")
            if type(domain) is str and domain in TRANSLATION_DOMAINS and type(key) is str and key in TRANSLATION_KEYS:
                if key not in result["translation_keys"]:
                    result["translation_keys"].append(key)

        for link in ("__cause__", "__context__"):
            linked = BaseException.__dict__[link].__get__(current)
            if isinstance(linked, BaseException) and id(linked) not in seen:
                pending.append(linked)
        if isinstance(current, BaseExceptionGroup):
            children = BaseExceptionGroup.__dict__["exceptions"].__get__(current)
            pending.extend(children[:MAX_EXCEPTION_NODES])
        # A malicious or very large group cannot grow the work list unboundedly.
        pending = pending[:MAX_EXCEPTION_NODES]

    result["category"] = next((category for category in ("timeout", "connection", "rejected")
                               if category in categories), "unknown")
    return result
