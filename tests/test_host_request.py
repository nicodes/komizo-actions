"""The frozen descriptor transports identities, never application secrets."""

import io
import json
import unittest

from host_admission.policy import Denied
from host_admission.protocol import VERSION
from host_admission.request import (
    MAX_REQUEST_BYTES,
    Request,
    decode_request,
    encode_request,
    read_request,
)


def descriptor():
    return dict(
        protocol=VERSION,
        request_id="a" * 32,
        candidate_manifest="sha256:" + "b" * 64,
        current_credential_revision="sha256:" + "c" * 64,
    )


class RequestTests(unittest.TestCase):
    def test_round_trip_is_deterministic(self):
        value = decode_request(json.dumps(descriptor()).encode())
        self.assertEqual(read_request(io.BytesIO(encode_request(value))), value)
        self.assertEqual(encode_request(value), encode_request(value))

    def test_exact_byte_limit_passes_one_over_denies(self):
        value = json.dumps(descriptor()).encode()
        padded = value + b" " * (MAX_REQUEST_BYTES - len(value))
        self.assertEqual(decode_request(padded), decode_request(value))
        self.assertEqual(read_request(io.BytesIO(padded)), decode_request(value))
        for payload in (padded + b" ", b" " * (MAX_REQUEST_BYTES + 1)):
            with self.assertRaises(Denied):
                read_request(io.BytesIO(payload))
            with self.assertRaises(Denied):
                decode_request(payload)

    def test_short_stream_reads_do_not_hide_trailing_data(self):
        class Short(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 7))

        value = json.dumps(descriptor()).encode()
        self.assertEqual(read_request(Short(value)), decode_request(value))
        with self.assertRaises(Denied):
            read_request(Short(value + b"{}"))

    def test_missing_and_unknown_fields(self):
        for key in descriptor():
            value = descriptor()
            del value[key]
            with self.subTest(key=key), self.assertRaises(Denied):
                decode_request(json.dumps(value).encode())
        for key in (
            "app",
            "path",
            "command",
            "secrets",
            "secret_updates",
            "registry_token",
            "policy",
            "env",
        ):
            value = descriptor() | {key: "SECRET_SENTINEL"}
            with self.subTest(key=key), self.assertRaises(Denied) as error:
                decode_request(json.dumps(value).encode())
            self.assertNotIn("SECRET_SENTINEL", str(error.exception))

    def test_duplicate_keys_including_escaped_spelling(self):
        payload = json.dumps(descriptor()).encode()
        for field in (b'"request_id"', b'"request_\\u0069d"'):
            with self.assertRaises(Denied):
                decode_request(payload[:-1] + b"," + field + b':"' + b"a" * 32 + b'"}')

    def test_rejects_wrong_types(self):
        for key in descriptor():
            for value in (None, True, 1, 1.5, [], {}):
                changed = descriptor() | {key: value}
                with self.subTest(key=key, value=value), self.assertRaises(Denied):
                    decode_request(json.dumps(changed).encode())

    def test_exact_lowercase_identities(self):
        for request_id in (
            "",
            "a" * 31,
            "a" * 33,
            "A" * 32,
            "../" + "a" * 29,
            "a" * 31 + "\n",
        ):
            with self.subTest(request_id=request_id), self.assertRaises(Denied):
                decode_request(
                    json.dumps(descriptor() | {"request_id": request_id}).encode()
                )
        for key in ("candidate_manifest", "current_credential_revision"):
            for value in (
                "sha256:" + "A" * 64,
                "a" * 64,
                "sha256:" + "a" * 63,
                "unknown",
            ):
                with self.subTest(key=key), self.assertRaises(Denied):
                    decode_request(json.dumps(descriptor() | {key: value}).encode())

    def test_unknown_protocol(self):
        with self.assertRaises(Denied):
            decode_request(
                json.dumps(
                    descriptor() | {"protocol": "komizo-host-admission/v2"}
                ).encode()
            )

    def test_invalid_utf8_bom_and_trailing_nonwhitespace(self):
        value = json.dumps(descriptor()).encode()
        for payload in (
            b"\xff",
            b"\xef\xbb\xbf" + value,
            value + b"{}",
            value + b"SECRET_SENTINEL",
            value + b"\x00",
        ):
            with self.subTest(payload=payload[:8]), self.assertRaises(Denied) as error:
                decode_request(payload)
            self.assertNotIn("SECRET_SENTINEL", str(error.exception))

    def test_nonobjects_and_nonjson_numbers(self):
        for payload in (
            b"[]",
            b"null",
            b"true",
            b"1",
            b'{"x":NaN}',
            b'{"x":Infinity}',
            b'{"x":' + b"1" * 100 + b"}",
        ):
            with self.assertRaises(Denied):
                decode_request(payload)

    def test_nested_input_fails_without_recursion_escape(self):
        with self.assertRaises(Denied):
            decode_request(b'{"x":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}")

    def test_binary_stream_required_and_read_errors_redacted(self):
        with self.assertRaises(Denied):
            read_request(io.StringIO(json.dumps(descriptor())))

        class Broken:
            def read(self, size):
                raise OSError("SECRET_SENTINEL")

        with self.assertRaises(Denied) as error:
            read_request(Broken())
        self.assertNotIn("SECRET_SENTINEL", str(error.exception))

    def test_encoder_rejects_invalid_manually_constructed_request(self):
        with self.assertRaises(Denied):
            encode_request(Request("bad", "bad", "SECRET_SENTINEL"))
        with self.assertRaises(Denied):
            encode_request(descriptor())

    def test_restored_valid_request_after_denial(self):
        with self.assertRaises(Denied):
            decode_request(b"{}")
        self.assertEqual(
            decode_request(json.dumps(descriptor()).encode()).request_id, "a" * 32
        )


if __name__ == "__main__":
    unittest.main()
