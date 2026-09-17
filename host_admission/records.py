"""Read-only adapter for private records installed by a trusted root importer.

This is NOT an importer, signature verifier, inventory collector, or admission
endpoint. Root must authenticate and retain the source evidence before import.
File ownership and digest syntax do not authenticate underlying measurements.
See docs/host-admission-records-v1.md for the required trust boundary.
"""

from dataclasses import dataclass
import os
import re
import stat

from .policy import Denied, Measurement, Release, count, digest, require
from .request import Request, _decode_object, _fields, _read_bounded


SCHEMA = "komizo-host-records/v1"
STORE = "/var/lib/komizo/admission/records"
MAX_RECORD_BYTES = 1024 * 1024
MAX_RELEASES = 256
MAX_FILESYSTEMS = 32


@dataclass(frozen=True)
class Provenance:
    producer: str
    verification_record: str
    verified_at: int
    expires_at: int


@dataclass(frozen=True)
class FilesystemEvidence:
    filesystem_id: str
    paths: tuple[str, ...]
    measurement: Measurement


@dataclass(frozen=True)
class Compatibility:
    release_manifest: str
    post_candidate_manifest: str
    post_candidate_schema: str
    post_candidate_credentials: str
    rollback_schema: str
    rollback_credentials: str
    evidence: str


@dataclass(frozen=True)
class Records:
    app: str
    platform: str
    candidate_manifest: str
    current_credential_revision: str
    provenance: Provenance
    current: str
    previous_successful: str
    releases: tuple[Release, ...]
    filesystems: tuple[FilesystemEvidence, ...]
    compatibility: Compatibility


def _app(value):
    require(
        type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value),
        "invalid registered app",
    )


def _platform(value):
    require(value in ("linux/amd64", "linux/arm64"), "unsupported platform")


def _array(value, maximum):
    require(type(value) is list and 0 < len(value) <= maximum, "invalid record array")


def _path(value):
    require(
        type(value) is str and re.fullmatch(r"/[A-Za-z0-9_./-]{0,254}", value),
        "invalid registered path",
    )
    require(
        value == "/" or all(p not in ("", ".", "..") for p in value[1:].split("/")),
        "noncanonical registered path",
    )


def _release(value, app, now):
    _fields(
        value,
        (
            "manifest",
            "app",
            "images",
            "config_image",
            "accepted_at",
            "schema",
            "credentials",
            "complete",
        ),
    )
    require(
        value["complete"] is True and value["app"] == app, "incomplete release record"
    )
    _array(value["images"], 256)
    for image in value["images"]:
        digest(image)
    require(
        len(set(value["images"])) == len(value["images"]), "duplicate release image"
    )
    release = Release(
        value["manifest"],
        app,
        frozenset(value["images"]),
        value["config_image"],
        value["accepted_at"],
        value["schema"],
        value["credentials"],
    )
    release.validate()
    require(
        release.accepted_at is None or release.accepted_at <= now,
        "future accepted release",
    )
    return release


def _decode_records(payload, app, platform, request, now):
    value = _decode_object(payload, MAX_RECORD_BYTES)
    _fields(
        value,
        (
            "schema",
            "app",
            "platform",
            "candidate_manifest",
            "current_credential_revision",
            "provenance",
            "history_complete",
            "current",
            "previous_successful",
            "releases",
            "filesystems",
            "compatibility",
        ),
    )
    require(value["schema"] == SCHEMA, "unsupported records schema")
    require(
        value["app"] == app and value["platform"] == platform,
        "record applicability mismatch",
    )
    require(
        value["candidate_manifest"] == request.candidate_manifest,
        "candidate record mismatch",
    )
    require(
        value["current_credential_revision"] == request.current_credential_revision,
        "credential record mismatch",
    )
    require(value["history_complete"] is True, "incomplete accepted history")
    source = value["provenance"]
    _fields(
        source, ("kind", "producer", "verification_record", "verified_at", "expires_at")
    )
    require(source["kind"] == "root-verified-import/v1", "unverified record source")
    require(
        type(source["producer"]) is str
        and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", source["producer"]),
        "invalid evidence producer",
    )
    digest(source["verification_record"])
    require(
        count(source["verified_at"]) <= now < count(source["expires_at"]),
        "record verification expired or future",
    )
    provenance = Provenance(
        source["producer"],
        source["verification_record"],
        source["verified_at"],
        source["expires_at"],
    )

    _array(value["releases"], MAX_RELEASES)
    releases = tuple(_release(r, app, now) for r in value["releases"])
    by_manifest = {r.manifest: r for r in releases}
    require(len(by_manifest) == len(releases), "duplicate release manifest")
    current = digest(value["current"])
    previous = digest(value["previous_successful"])
    require(current != previous, "previous release must be distinct")
    for manifest in (current, previous):
        require(
            manifest in by_manifest and by_manifest[manifest].accepted_at is not None,
            "missing successful release",
        )
    require(
        sum(r.accepted_at is not None for r in releases) >= 3,
        "fewer than three accepted releases",
    )
    candidate = by_manifest.get(request.candidate_manifest)
    require(
        candidate is not None and candidate.accepted_at is None,
        "missing pending candidate",
    )
    for release in (candidate, by_manifest[current], by_manifest[previous]):
        require(
            release.credentials == request.current_credential_revision,
            "credential changes are unsupported",
        )

    _array(value["filesystems"], MAX_FILESYSTEMS)
    filesystems = []
    seen_ids = set()
    seen_paths = set()
    for item in value["filesystems"]:
        _fields(item, ("filesystem_id", "paths", "measurement"))
        identity = digest(item["filesystem_id"])
        require(identity not in seen_ids, "duplicate filesystem evidence")
        seen_ids.add(identity)
        _array(item["paths"], 32)
        for path in item["paths"]:
            _path(path)
            require(path not in seen_paths, "duplicate filesystem path")
            seen_paths.add(path)
        m = item["measurement"]
        _fields(
            m,
            (
                "candidate_manifest",
                "candidate_evidence",
                "write_evidence",
                "candidate_peak_bytes",
                "candidate_peak_inodes",
                "write_30m_bytes",
                "write_30m_inodes",
                "write_window_seconds",
            ),
        )
        measurement = Measurement(**m)
        measurement.validate(request.candidate_manifest)
        filesystems.append(
            FilesystemEvidence(identity, tuple(item["paths"]), measurement)
        )

    c = value["compatibility"]
    _fields(
        c,
        (
            "release_manifest",
            "post_candidate_manifest",
            "post_candidate_schema",
            "post_candidate_credentials",
            "rollback_schema",
            "rollback_credentials",
            "evidence",
        ),
    )
    for entry in c.values():
        digest(entry)
    require(
        c["release_manifest"] == previous
        and c["post_candidate_manifest"] == candidate.manifest
        and c["post_candidate_schema"] == candidate.schema
        and c["post_candidate_credentials"] == candidate.credentials
        and c["rollback_schema"] == by_manifest[previous].schema
        and c["rollback_credentials"] == by_manifest[previous].credentials,
        "directional compatibility record mismatch",
    )
    return Records(
        app,
        platform,
        request.candidate_manifest,
        request.current_credential_revision,
        provenance,
        current,
        previous,
        releases,
        tuple(filesystems),
        Compatibility(**c),
    )


def _directory(fd, private=False):
    info = os.fstat(fd)
    require(
        stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and info.st_gid == 0,
        "unsafe record directory",
    )
    mode = stat.S_IMODE(info.st_mode)
    require(
        mode == 0o700 if private else not mode & 0o022,
        "unsafe record directory permissions",
    )


def _read_record(app, manifest):
    """Walk fixed root-owned ancestors by descriptor; never follow any symlink."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", directory_flags)
    try:
        _directory(fd)
        parts = STORE.strip("/").split("/") + [app]
        for index, part in enumerate(parts):
            child = os.open(part, directory_flags, dir_fd=fd)
            os.close(fd)
            fd = child
            # Admission root, records directory, and app directory are private.
            _directory(fd, private=index >= len(parts) - 3)
        name = manifest.removeprefix("sha256:") + ".json"
        record_fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
        )
        with os.fdopen(record_fd, "rb") as source:
            before = os.fstat(source.fileno())
            require(
                stat.S_ISREG(before.st_mode)
                and before.st_uid == 0
                and before.st_gid == 0
                and stat.S_IMODE(before.st_mode) == 0o600
                and before.st_nlink == 1
                and before.st_size <= MAX_RECORD_BYTES,
                "unsafe record file",
            )
            payload = _read_bounded(source, MAX_RECORD_BYTES)
            after = os.fstat(source.fileno())
            require(
                (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                == (after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                "record changed while reading",
            )
            return payload
    finally:
        os.close(fd)


def load_records(app, platform, request, now):
    """Load from the fixed root-import store, never a request-supplied path.

    Arguments app/platform/time are trusted host context, not request overrides.
    Returned records still require fresh inventory, retained evidence verification,
    actual post-candidate state checks and ALL existing policy checks under lock.
    """
    _app(app)
    _platform(platform)
    require(type(request) is Request, "request required")
    request.validate()
    count(now)
    try:
        return _decode_records(
            _read_record(app, request.candidate_manifest), app, platform, request, now
        )
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise Denied("trusted records unavailable or invalid") from None
