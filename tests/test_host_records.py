"""Root metadata is simulated; file/FIFO/symlink operations are real and local."""

import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from host_admission import records
from host_admission.policy import Denied, Measurement, Release
from host_admission.request import Request


def sha(number):
    return "sha256:" + f"{number:064x}"


def bundle():
    return dict(
        schema=records.SCHEMA,
        app="demo",
        platform="linux/amd64",
        candidate_manifest=sha(4),
        current_credential_revision=sha(99),
        provenance=dict(
            kind="root-verified-import/v1",
            producer="approved-recorder",
            verification_record=sha(90),
            verified_at=100,
            expires_at=200,
        ),
        history_complete=True,
        current=sha(3),
        previous_successful=sha(2),
        releases=[
            dict(
                manifest=sha(i),
                app="demo",
                images=[sha(100 + i), sha(200 + i)],
                config_image=sha(200 + i),
                accepted_at=i if i < 4 else None,
                schema=sha(300 + i),
                credentials=sha(99),
                complete=True,
            )
            for i in range(1, 5)
        ],
        filesystems=[
            dict(
                filesystem_id=sha(80),
                paths=["/var/lib/docker", "/srv/demo"],
                measurement=dict(
                    candidate_manifest=sha(4),
                    candidate_evidence=sha(81),
                    write_evidence=sha(82),
                    candidate_peak_bytes=1024,
                    candidate_peak_inodes=4,
                    write_30m_bytes=2048,
                    write_30m_inodes=5,
                    write_window_seconds=1800,
                ),
            )
        ],
        compatibility=dict(
            release_manifest=sha(2),
            post_candidate_manifest=sha(4),
            post_candidate_schema=sha(304),
            post_candidate_credentials=sha(99),
            rollback_schema=sha(302),
            rollback_credentials=sha(99),
            evidence=sha(83),
        ),
    )


REQUEST = Request("a" * 32, sha(4), sha(99))


class RecordSchemaTests(unittest.TestCase):
    def decode(self, value, **changes):
        context = (
            dict(app="demo", platform="linux/amd64", request=REQUEST, now=150) | changes
        )
        return records._decode_records(json.dumps(value).encode(), **context)

    def test_adapts_complete_bound_records_to_policy_types(self):
        value = self.decode(bundle())
        self.assertIsInstance(value.releases[0], Release)
        self.assertIsInstance(value.filesystems[0].measurement, Measurement)
        self.assertEqual(value.previous_successful, sha(2))
        self.assertEqual(value.compatibility.post_candidate_schema, sha(304))
        self.assertEqual(value.filesystems[0].paths, ("/var/lib/docker", "/srv/demo"))

    def test_app_platform_candidate_and_credential_bindings(self):
        for change in (
            {"app": "other"},
            {"platform": "linux/arm64"},
            {"schema": "unknown"},
            {"candidate_manifest": sha(9)},
            {"current_credential_revision": sha(9)},
        ):
            with self.subTest(change=change), self.assertRaises(Denied):
                self.decode(bundle() | change)

    def test_unverified_expired_future_and_missing_provenance(self):
        for change in (
            {"kind": "caller-asserted"},
            {"verified_at": 151},
            {"expires_at": 150},
            {"verification_record": ""},
            {"producer": ""},
            {"verified_at": True},
        ):
            value = bundle()
            value["provenance"].update(change)
            with self.subTest(change=change), self.assertRaises(Denied):
                self.decode(value)
        value = bundle()
        del value["provenance"]
        with self.assertRaises(Denied):
            self.decode(value)

    def test_current_previous_and_three_set_floor(self):
        for change in (
            {"previous_successful": sha(3)},
            {"previous_successful": sha(9)},
            {"current": sha(4)},
            {"releases": bundle()["releases"][1:]},
            {"history_complete": False},
            {"history_complete": 1},
        ):
            with self.subTest(change=change), self.assertRaises(Denied):
                self.decode(bundle() | change)

    def test_incomplete_config_images_duplicate_or_foreign_releases(self):
        for change in (
            {"complete": False},
            {"complete": 1},
            {"app": "other"},
            {"images": []},
            {"images": [sha(101), sha(101)]},
            {"config_image": sha(9)},
            {"accepted_at": 151},
        ):
            value = bundle()
            value["releases"][0].update(change)
            with self.subTest(change=change), self.assertRaises(Denied):
                self.decode(value)
        value = bundle()
        value["releases"].append(value["releases"][0])
        with self.assertRaises(Denied):
            self.decode(value)

    def test_pending_candidate_required(self):
        for accepted in (4, True):
            value = bundle()
            value["releases"][3]["accepted_at"] = accepted
            with self.assertRaises(Denied):
                self.decode(value)
        value = bundle()
        value["releases"].pop()
        with self.assertRaises(Denied):
            self.decode(value)

    def test_unchanged_credentials_for_current_previous_candidate(self):
        for index in (1, 2, 3):
            value = bundle()
            value["releases"][index]["credentials"] = sha(98)
            with self.subTest(index=index), self.assertRaises(Denied):
                self.decode(value)

    def test_direction_specific_compatibility(self):
        for field in bundle()["compatibility"]:
            value = bundle()
            value["compatibility"][field] = sha(9) if field != "evidence" else ""
            with self.subTest(field=field), self.assertRaises(Denied):
                self.decode(value)

    def test_measurement_identity_window_and_numeric_evidence(self):
        for change in (
            {"candidate_manifest": sha(9)},
            {"candidate_evidence": ""},
            {"write_evidence": ""},
            {"write_window_seconds": 1799},
            {"candidate_peak_bytes": True},
            {"write_30m_inodes": -1},
        ):
            value = bundle()
            value["filesystems"][0]["measurement"].update(change)
            with self.subTest(change=change), self.assertRaises(Denied):
                self.decode(value)

    def test_filesystem_identity_paths_and_limits(self):
        for change in (
            {"filesystem_id": ""},
            {"paths": []},
            {"paths": ["/srv/../root"]},
            {"paths": ["/srv/demo", "/srv/demo"]},
            {"paths": ["/srv/demo\nSECRET_SENTINEL"]},
        ):
            value = bundle()
            value["filesystems"][0].update(change)
            with self.subTest(change=change), self.assertRaises(Denied):
                self.decode(value)
        for field, maximum in (("filesystems", 32), ("releases", 256)):
            value = bundle()
            value[field] = value[field][:1] * (maximum + 1)
            with self.assertRaises(Denied):
                self.decode(value)
        value = bundle()
        value["filesystems"] *= 2
        with self.assertRaises(Denied):
            self.decode(value)

    def test_unknown_secret_fields_rejected_at_each_level(self):
        for target in (
            "top",
            "provenance",
            "release",
            "filesystem",
            "measurement",
            "compatibility",
        ):
            value = bundle()
            obj = {
                "top": value,
                "provenance": value["provenance"],
                "release": value["releases"][0],
                "filesystem": value["filesystems"][0],
                "measurement": value["filesystems"][0]["measurement"],
                "compatibility": value["compatibility"],
            }[target]
            obj["secret_values"] = "SECRET_SENTINEL"
            with self.subTest(target=target), self.assertRaises(Denied) as error:
                self.decode(value)
            self.assertNotIn("SECRET_SENTINEL", str(error.exception))

    def test_duplicate_json_fields_deny(self):
        payload = json.dumps(bundle()).encode()
        with self.assertRaises(Denied):
            records._decode_records(
                payload[:-1] + b',"app":"demo"}', "demo", "linux/amd64", REQUEST, 150
            )

    def test_restored_records_after_denial(self):
        with self.assertRaises(Denied):
            self.decode(bundle() | {"releases": []})
        self.assertEqual(self.decode(bundle()).candidate_manifest, sha(4))


class RecordFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = self.root / "admission" / "records"
        self.app_dir = self.store / "demo"
        self.app_dir.mkdir(parents=True)
        for path in (self.root / "admission", self.store, self.app_dir):
            path.chmod(0o700)
        self.file = self.app_dir / (sha(4)[7:] + ".json")
        self.file.write_text(json.dumps(bundle()))
        self.file.chmod(0o600)
        self.real_fstat = os.fstat

    def tearDown(self):
        self.temp.cleanup()

    def root_stat(self, fd):
        info = self.real_fstat(fd)
        return SimpleNamespace(
            st_uid=0,
            st_gid=0,
            **{
                key: getattr(info, key)
                for key in (
                    "st_mode",
                    "st_size",
                    "st_nlink",
                    "st_mtime_ns",
                    "st_ctime_ns",
                )
            },
        )

    def load(self):
        # Real no-follow path traversal and file access, but no need for root:
        # directory ownership is tested independently below; file owners simulated.
        with patch.object(records, "STORE", str(self.store)), patch.object(
            records, "_directory"
        ), patch.object(records.os, "fstat", side_effect=self.root_stat):
            return records.load_records("demo", "linux/amd64", REQUEST, 150)

    def test_real_file_load_and_restore(self):
        self.assertEqual(self.load().current, sha(3))
        self.file.unlink()
        with self.assertRaises(Denied):
            self.load()
        self.file.write_text(json.dumps(bundle()))
        self.file.chmod(0o600)
        self.assertEqual(self.load().previous_successful, sha(2))

    def test_private_file_mode_and_hardlink(self):
        self.file.chmod(0o640)
        with self.assertRaises(Denied):
            self.load()
        self.file.chmod(0o600)
        os.link(self.file, self.app_dir / "link")
        with self.assertRaises(Denied):
            self.load()

    def test_final_symlink_and_fifo_never_read(self):
        original = self.app_dir / "original"
        self.file.rename(original)
        self.file.symlink_to(original)
        with self.assertRaises(Denied):
            self.load()
        self.file.unlink()
        os.mkfifo(self.file, 0o600)
        with self.assertRaises(Denied):
            self.load()

    def test_symlink_ancestor_denied(self):
        actual = self.store / "actual"
        self.app_dir.rename(actual)
        self.app_dir.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(Denied):
            self.load()

    def test_oversize_and_invalid_json_redacted(self):
        self.file.write_bytes(b"x" * (records.MAX_RECORD_BYTES + 1))
        with self.assertRaises(Denied):
            self.load()
        self.file.write_bytes(b"SECRET_SENTINEL")
        with self.assertRaises(Denied) as error:
            self.load()
        self.assertNotIn("SECRET_SENTINEL", str(error.exception))

    def test_root_ownership_and_safe_parent_permissions(self):
        fd = os.open(self.app_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with patch.object(records.os, "fstat", side_effect=self.root_stat):
                records._directory(fd, private=True)
                self.app_dir.chmod(0o750)
                with self.assertRaises(Denied):
                    records._directory(fd, private=True)
                records._directory(fd)
                self.app_dir.chmod(0o770)
                with self.assertRaises(Denied):
                    records._directory(fd)
            info = self.root_stat(fd)
            info.st_uid = 1
            with patch.object(
                records.os, "fstat", return_value=info
            ), self.assertRaises(Denied):
                records._directory(fd)
        finally:
            os.close(fd)

    def test_untrusted_file_owner_denied(self):
        def other_owner(fd):
            info = self.root_stat(fd)
            info.st_uid = 1
            return info

        with patch.object(records, "STORE", str(self.store)), patch.object(
            records, "_directory"
        ), patch.object(records.os, "fstat", side_effect=other_owner):
            with self.assertRaises(Denied):
                records.load_records("demo", "linux/amd64", REQUEST, 150)

    def test_in_place_change_during_read_denied(self):
        calls = 0

        def changing(fd):
            nonlocal calls
            info = self.root_stat(fd)
            calls += 1
            if calls == 2:
                info.st_mtime_ns += 1
            return info

        with patch.object(records, "STORE", str(self.store)), patch.object(
            records, "_directory"
        ), patch.object(records.os, "fstat", side_effect=changing):
            with self.assertRaises(Denied):
                records.load_records("demo", "linux/amd64", REQUEST, 150)

    def test_bad_host_context_rejected_before_open(self):
        for app, platform, request, now in (
            ("../other", "linux/amd64", REQUEST, 150),
            ("demo", "unknown", REQUEST, 150),
            ("demo", "linux/amd64", {}, 150),
            ("demo", "linux/amd64", REQUEST, True),
        ):
            with patch.object(records, "_read_record") as read, self.assertRaises(
                Denied
            ):
                records.load_records(app, platform, request, now)
            read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
