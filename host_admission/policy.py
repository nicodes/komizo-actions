"""Pure checks for root-collected, lock-scoped evidence (protocol v1).

This module does not collect evidence, acquire locks, authenticate callers, delete
images, or authorize deployment. A successful evaluation is necessary but NOT
sufficient for admission. See docs/host-admission-v1.md before integrating it.
"""

from dataclasses import dataclass
import re


GIB = 1024**3
WEEK = 7 * 24 * 60 * 60
LOCK_WAIT_SECONDS = 600
TRANSACTION_SECONDS = 1200
RECOVERY_SECONDS = 600


class Denied(ValueError):
    """Evidence is incomplete, inconsistent, or outside the approved policy."""


def require(condition, reason):
    if not condition:
        raise Denied(reason)


def count(value):
    # bool is an int subclass; evidence must not silently turn true into 1.
    require(type(value) is int and value >= 0, "invalid nonnegative count")
    return value


def digest(value):
    require(
        isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value),
        "missing or invalid immutable digest",
    )
    return value


@dataclass(frozen=True)
class Filesystem:
    """Capacity available to workloads, not reserved filesystem blocks."""

    capacity_bytes: int
    available_bytes: int
    capacity_inodes: int
    available_inodes: int

    def validate(self):
        for capacity, available in (
            (self.capacity_bytes, self.available_bytes),
            (self.capacity_inodes, self.available_inodes),
        ):
            require(count(capacity) > 0, "unknown filesystem capacity")
            require(count(available) <= capacity, "inconsistent filesystem inventory")


@dataclass(frozen=True)
class Measurement:
    """Trusted evidence for this exact candidate and representative write window.

    Evidence digests identify retained measurement records, not signatures. The
    adapter must verify provenance and applicability; a caller's numbers do not
    become trustworthy merely because a digest is syntactically valid.
    """

    candidate_manifest: str
    candidate_evidence: str
    write_evidence: str
    candidate_peak_bytes: int
    candidate_peak_inodes: int
    write_30m_bytes: int
    write_30m_inodes: int
    write_window_seconds: int

    def validate(self, candidate_manifest):
        for value in (
            self.candidate_manifest,
            self.candidate_evidence,
            self.write_evidence,
        ):
            digest(value)
        require(
            self.candidate_manifest == digest(candidate_manifest), "candidate mismatch"
        )
        require(
            type(self.write_window_seconds) is int
            and self.write_window_seconds == 1800,
            "representative 30-minute write evidence required",
        )
        for value in (
            self.candidate_peak_bytes,
            self.candidate_peak_inodes,
            self.write_30m_bytes,
            self.write_30m_inodes,
        ):
            count(value)


@dataclass(frozen=True)
class Headroom:
    required_bytes: int
    required_inodes: int


def check_headroom(filesystem, measurement, candidate_manifest):
    """Check one filesystem; adapters must cover EVERY affected filesystem.

    Rounded upward using integer arithmetic, including at the exact threshold.
    No deduction for reclaimable images: eligibility is not deletion authority.
    """
    filesystem.validate()
    measurement.validate(candidate_manifest)
    required_bytes = (
        max(5 * GIB, (filesystem.capacity_bytes + 4) // 5)
        + (3 * measurement.candidate_peak_bytes + 1) // 2
        + max(GIB, 2 * measurement.write_30m_bytes)
    )
    required_inodes = (
        max(100_000, (filesystem.capacity_inodes + 9) // 10)
        + (3 * measurement.candidate_peak_inodes + 1) // 2
        + 2 * measurement.write_30m_inodes
    )
    require(filesystem.available_bytes >= required_bytes, "insufficient byte headroom")
    require(
        filesystem.available_inodes >= required_inodes, "insufficient inode headroom"
    )
    return Headroom(required_bytes, required_inodes)


@dataclass(frozen=True)
class Release:
    """Root-recorded complete release set, including all profiles and config."""

    manifest: str
    app: str
    images: frozenset[str]
    config_image: str
    accepted_at: int | None
    schema: str
    credentials: str  # Opaque revision identity only; NEVER credential contents.

    def validate(self):
        digest(self.manifest)
        require(
            isinstance(self.app, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", self.app),
            "invalid app identity",
        )
        require(
            type(self.images) is frozenset and self.images, "empty release image set"
        )
        for image in self.images:
            digest(image)
        require(
            digest(self.config_image) in self.images, "config image missing from set"
        )
        digest(self.schema)
        digest(self.credentials)
        if self.accepted_at is not None:
            count(self.accepted_at)


@dataclass(frozen=True)
class Inventory:
    releases: tuple[Release, ...]
    current: dict[str, str]
    previous_successful: dict[str, str]
    local_images: frozenset[str]
    container_images: frozenset[str]  # Running AND stopped containers, every app.
    pending_images: frozenset[str]
    pinned_images: frozenset[str]
    unknown_owner_images: frozenset[str]
    complete: bool


@dataclass(frozen=True)
class Protection:
    release_manifests: frozenset[str]
    images: frozenset[str]
    eligible_images: frozenset[str]  # Informational only; NEVER delete automatically.


def protected_sets(inventory, now):
    """Union protection; missing history/metadata denies instead of guessing.

    v1 deliberately has no bootstrap exception for fewer than three accepted
    sets. Such a host requires an explicit future policy decision, not a bypass.
    """
    count(now)
    require(inventory.complete is True, "incomplete host inventory")
    require(bool(inventory.current), "missing app inventory")
    require(
        inventory.current.keys() == inventory.previous_successful.keys(),
        "missing previous successful release",
    )
    by_manifest = {}
    for release in inventory.releases:
        release.validate()
        require(release.manifest not in by_manifest, "duplicate release manifest")
        require(release.app in inventory.current, "unmapped application")
        if release.accepted_at is not None:
            require(release.accepted_at <= now, "future acceptance timestamp")
        by_manifest[release.manifest] = release

    protected = set()
    for app, current in inventory.current.items():
        previous = inventory.previous_successful[app]
        require(current != previous, "previous successful release must be distinct")
        for manifest in (current, previous):
            release = by_manifest.get(manifest)
            require(
                release is not None
                and release.app == app
                and release.accepted_at is not None,
                "missing complete current/previous successful manifest",
            )
            protected.add(manifest)
        accepted = sorted(
            (
                r
                for r in inventory.releases
                if r.app == app and r.accepted_at is not None
            ),
            key=lambda r: (r.accepted_at, r.manifest),
            reverse=True,
        )
        require(len(accepted) >= 3, "fewer than three accepted release sets")
        protected.update(r.manifest for r in accepted[:3])
        protected.update(r.manifest for r in accepted if r.accepted_at >= now - WEEK)
        # Not-yet-accepted releases are pending; never infer abandonment from age.
        protected.update(
            r.manifest
            for r in inventory.releases
            if r.app == app and r.accepted_at is None
        )

    images = set()
    for manifest in protected:
        images.update(by_manifest[manifest].images)
    require(
        images <= inventory.local_images, "protected release set not complete locally"
    )
    for references in (
        inventory.local_images,
        inventory.container_images,
        inventory.pending_images,
        inventory.pinned_images,
        inventory.unknown_owner_images,
    ):
        require(type(references) is frozenset, "invalid image inventory")
        for image in references:
            digest(image)
    for references in (
        inventory.container_images,
        inventory.pending_images,
        inventory.pinned_images,
        inventory.unknown_owner_images,
    ):
        images.update(references)
    return Protection(
        frozenset(protected), frozenset(images), inventory.local_images - images
    )


def check_offline_restore(release, local_images, config_manifests, compatibility):
    """Validate required evidence, not image content or actual database state.

    compatibility contains trusted, direction-specific (manifest, schema,
    credential-revision) tuples validated against the POST-candidate state. It
    is not a caller-provided boolean and cannot be inferred from keeping images.
    """
    release.validate()
    require(release.accepted_at is not None, "rollback target was not accepted")
    require(release.images <= local_images, "offline image set incomplete")
    require(release.manifest in config_manifests, "offline config manifest missing")
    require(
        (release.manifest, release.schema, release.credentials) in compatibility,
        "post-candidate schema/credential compatibility unproven",
    )
