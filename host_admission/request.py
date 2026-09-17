"""Bounded, nonsecret v1 submit descriptors; not an admission endpoint."""

from dataclasses import dataclass
import json
import re

from .policy import Denied, digest, require
from .protocol import VERSION


MAX_REQUEST_BYTES = 1024 * 1024


def _fields(value, names):
    require(type(value) is dict and value.keys() == set(names), "invalid object fields")


def _read_bounded(stream, maximum):
    chunks = []
    size = 0
    try:
        while size <= maximum:
            chunk = stream.read(min(65536, maximum + 1 - size))
            require(type(chunk) is bytes, "binary input required")
            if not chunk:
                return b"".join(chunks)
            size += len(chunk)
            require(size <= maximum, "input exceeds byte limit")
            chunks.append(chunk)
    except (OSError, TypeError, ValueError):
        raise Denied("input could not be read") from None
    raise Denied("input exceeds byte limit")


def _decode_object(payload, maximum):
    require(type(payload) is bytes and len(payload) <= maximum, "invalid input size")

    def unique(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate object field")
            result[key] = value
        return result

    def integer(value):
        require(len(value) <= 20, "integer exceeds limit")
        return int(value)

    def constant(_):
        raise Denied("non-JSON constant")

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique,
            parse_int=integer,
            parse_constant=constant,
        )
        require(type(value) is dict, "object required")
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        # Never include supplied values, decoder context, or secret canaries.
        raise Denied("invalid JSON object") from None


@dataclass(frozen=True)
class Request:
    request_id: str
    candidate_manifest: str
    current_credential_revision: str

    def validate(self):
        require(
            type(self.request_id) is str
            and re.fullmatch(r"[0-9a-f]{32}", self.request_id),
            "invalid request identity",
        )
        digest(self.candidate_manifest)
        digest(self.current_credential_revision)


def decode_request(payload):
    """Decode exactly four fields. Application identity never comes from JSON."""
    value = _decode_object(payload, MAX_REQUEST_BYTES)
    _fields(
        value,
        ("protocol", "request_id", "candidate_manifest", "current_credential_revision"),
    )
    require(value["protocol"] == VERSION, "unsupported request protocol")
    result = Request(
        value["request_id"],
        value["candidate_manifest"],
        value["current_credential_revision"],
    )
    result.validate()
    return result


def read_request(stream):
    """Read a binary stream to EOF, bounded to 1 MiB including whitespace."""
    return decode_request(_read_bounded(stream, MAX_REQUEST_BYTES))


def encode_request(request):
    """Produce deterministic nonsecret JSON; invalid manually built values deny."""
    require(type(request) is Request, "request required")
    request.validate()
    return json.dumps(
        dict(
            protocol=VERSION,
            request_id=request.request_id,
            candidate_manifest=request.candidate_manifest,
            current_credential_revision=request.current_credential_revision,
        ),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
