"""Pure contract controls; these do not simulate a functioning host adapter."""

import copy
from dataclasses import replace
import unittest

from host_admission.policy import (
    Denied,
    Filesystem,
    GIB,
    Inventory,
    Measurement,
    Release,
    WEEK,
    check_headroom,
    check_offline_restore,
    protected_sets,
)
from host_admission.protocol import CAPABILITIES, VERSION, check_capability


def sha(number):
    return "sha256:" + f"{number:064x}"


def release(number, app="blog", accepted_at=1):
    return Release(
        sha(number),
        app,
        frozenset({sha(100 + number), sha(200 + number), sha(999)}),
        sha(200 + number),
        accepted_at,
        sha(300 + number),
        sha(400 + number),
    )


class HeadroomTests(unittest.TestCase):
    def setUp(self):
        self.measurement = Measurement(
            sha(1), sha(2), sha(3), 2 * GIB, 100, 0, 10, 1800
        )
        self.filesystem = Filesystem(20 * GIB, 9 * GIB, 1_000_000, 100_170)

    def test_exact_bound_and_restored_capacity(self):
        result = check_headroom(self.filesystem, self.measurement, sha(1))
        self.assertEqual(
            (result.required_bytes, result.required_inodes), (9 * GIB, 100_170)
        )
        for field in ("available_bytes", "available_inodes"):
            for value in (0, getattr(self.filesystem, field) - 1):
                with self.subTest(field=field, value=value), self.assertRaises(Denied):
                    check_headroom(
                        replace(self.filesystem, **{field: value}),
                        self.measurement,
                        sha(1),
                    )
            self.assertEqual(
                check_headroom(self.filesystem, self.measurement, sha(1)), result
            )

    def test_percentage_reserve_and_write_bound(self):
        fs = Filesystem(100 * GIB, 100 * GIB, 10_000_000, 10_000_000)
        measurement = replace(
            self.measurement, write_30m_bytes=2 * GIB, write_30m_inodes=100
        )
        result = check_headroom(fs, measurement, sha(1))
        self.assertEqual(
            (result.required_bytes, result.required_inodes), (27 * GIB, 1_000_350)
        )

    def test_rounds_up_each_margin_without_float(self):
        fs = Filesystem(100 * GIB + 1, 100 * GIB, 10_000_001, 10_000_000)
        measurement = replace(
            self.measurement, candidate_peak_bytes=1, candidate_peak_inodes=1
        )
        result = check_headroom(fs, measurement, sha(1))
        self.assertEqual(result.required_bytes, 21 * GIB + 3)
        self.assertEqual(result.required_inodes, 1_000_023)

    def test_unknown_invalid_and_wrong_candidate_evidence(self):
        for field in ("candidate_evidence", "write_evidence", "candidate_manifest"):
            with self.subTest(field=field), self.assertRaises(Denied):
                check_headroom(
                    self.filesystem, replace(self.measurement, **{field: ""}), sha(1)
                )
        for value in (None, True, -1, 1.5):
            with self.subTest(value=value), self.assertRaises(Denied):
                check_headroom(
                    self.filesystem,
                    replace(self.measurement, candidate_peak_bytes=value),
                    sha(1),
                )
        with self.assertRaises(Denied):
            check_headroom(self.filesystem, self.measurement, sha(4))
        with self.assertRaises(Denied):
            check_headroom(
                self.filesystem,
                replace(self.measurement, write_window_seconds=1799),
                sha(1),
            )
        for fs in (
            replace(self.filesystem, capacity_inodes=0),
            replace(self.filesystem, available_bytes=21 * GIB),
        ):
            with self.assertRaises(Denied):
                check_headroom(fs, self.measurement, sha(1))


class ProtectionTests(unittest.TestCase):
    def setUp(self):
        self.now = 10 * WEEK
        # Current/previous are old; newer accepted sets independently need protection.
        self.releases = tuple(release(n, accepted_at=n) for n in range(1, 7)) + (
            release(7, accepted_at=self.now - WEEK),
            release(8, accepted_at=self.now - 1),
            release(9, accepted_at=None),
        )
        local = frozenset().union(*(r.images for r in self.releases), {sha(900)})
        self.inventory = Inventory(
            self.releases,
            {"blog": sha(1)},
            {"blog": sha(2)},
            local,
            frozenset({sha(901), sha(902)}),
            frozenset({sha(903)}),
            frozenset({sha(904)}),
            frozenset({sha(905)}),
            True,
        )

    def test_union_old_current_previous_week_minimum_three_pending_and_references(self):
        result = protected_sets(self.inventory, self.now)
        self.assertEqual(
            result.release_manifests, frozenset(sha(n) for n in (1, 2, 6, 7, 8, 9))
        )
        self.assertTrue({sha(n) for n in range(901, 906)} <= result.images)
        self.assertIn(sha(999), result.images)  # Shared with unprotected releases.
        self.assertIn(sha(900), result.eligible_images)
        self.assertNotIn(sha(999), result.eligible_images)

    def test_two_apps_get_independent_minimum_and_combined_image_protection(self):
        other = tuple(release(n, "shop", n) for n in range(10, 14))
        inventory = replace(
            self.inventory,
            releases=self.releases + other,
            current={"blog": sha(1), "shop": sha(10)},
            previous_successful={"blog": sha(2), "shop": sha(11)},
            local_images=self.inventory.local_images.union(*(r.images for r in other)),
        )
        result = protected_sets(inventory, self.now)
        self.assertTrue({sha(n) for n in range(10, 14)} <= result.release_manifests)

    def test_missing_image_is_denied_then_restored(self):
        for image in self.releases[1].images:
            with self.subTest(image=image), self.assertRaises(Denied):
                protected_sets(
                    replace(
                        self.inventory,
                        local_images=self.inventory.local_images - {image},
                    ),
                    self.now,
                )
        protected_sets(self.inventory, self.now)

    def test_missing_history_and_unknown_inventory_fail_closed(self):
        for change in (
            {"complete": False},
            {"complete": 1},
            {"previous_successful": {}},
            {"previous_successful": {"blog": sha(1)}},
            {"releases": self.releases[:2]},
            {"releases": self.releases + (self.releases[0],)},
            {"releases": self.releases + (release(99, "unknown"),)},
            {"releases": self.releases + (release(99, accepted_at=self.now + 1),)},
        ):
            with self.subTest(change=change), self.assertRaises(Denied):
                protected_sets(replace(self.inventory, **change), self.now)


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.release = release(1)
        self.compatibility = {
            (self.release.manifest, self.release.schema, self.release.credentials)
        }

    def check(self, **changes):
        args = dict(
            release=self.release,
            local_images=self.release.images,
            config_manifests={self.release.manifest},
            compatibility=self.compatibility,
        )
        args.update(changes)
        check_offline_restore(**args)

    def test_complete_partial_and_restored(self):
        self.check()
        for image in self.release.images:
            with self.subTest(image=image), self.assertRaises(Denied):
                self.check(local_images=self.release.images - {image})
        with self.assertRaises(Denied):
            self.check(config_manifests=set())
        self.check()

    def test_schema_and_credential_compatibility_are_not_inferred_from_images(self):
        for compatibility in (
            set(),
            {(self.release.manifest, sha(888), self.release.credentials)},
            {(self.release.manifest, self.release.schema, sha(888))},
        ):
            with self.subTest(compatibility=compatibility), self.assertRaises(Denied):
                self.check(compatibility=compatibility)
        self.check()

    def test_incomplete_config_or_unaccepted_target(self):
        for candidate in (
            replace(
                self.release, images=self.release.images - {self.release.config_image}
            ),
            replace(self.release, accepted_at=None),
        ):
            with self.assertRaises(Denied):
                self.check(release=candidate)


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.evidence = dict(
            protocol=VERSION,
            capabilities=sorted(CAPABILITIES),
            lock_wait_seconds=600,
            transaction_seconds=1200,
            recovery_seconds=600,
        )

    def test_known_contract_and_restored(self):
        check_capability(self.evidence)
        for field in self.evidence:
            evidence = copy.deepcopy(self.evidence)
            del evidence[field]
            with self.subTest(field=field), self.assertRaises(Denied):
                check_capability(evidence)
        check_capability(self.evidence)

    def test_unknown_missing_lock_and_unbounded_timing(self):
        for changes in (
            {"protocol": "legacy"},
            {"extra": True},
            {"lock_wait_seconds": 0},
            {"transaction_seconds": 1800},
            {"recovery_seconds": None},
            {"capabilities": sorted(CAPABILITIES - {"mandatory-global-lock"})},
            {"capabilities": sorted(CAPABILITIES - {"all-writers-participate"})},
            {"capabilities": sorted(CAPABILITIES) + ["unknown"]},
            {"capabilities": sorted(CAPABILITIES) + ["mandatory-global-lock"]},
        ):
            with self.subTest(changes=changes), self.assertRaises(Denied):
                check_capability(self.evidence | changes)


if __name__ == "__main__":
    unittest.main()
