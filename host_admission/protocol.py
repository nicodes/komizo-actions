"""v1 capability contract, not a host implementation or a preflight permit."""

from .policy import (
    LOCK_WAIT_SECONDS,
    RECOVERY_SECONDS,
    TRANSACTION_SECONDS,
    require,
)


VERSION = "komizo-host-admission/v1"
CAPABILITIES = frozenset(
    {
        "mandatory-global-lock",
        "all-writers-participate",
        "fresh-inventory-under-lock",
        "measured-byte-and-inode-bounds",
        "complete-release-set-protection",
        "staged-secrets-after-admission",
        "recheck-before-activation",
        "health-under-lock",
        "offline-complete-restore",
        "post-candidate-compatibility",
        "disconnect-safe-recovery",
        "no-automatic-deletion",
    }
)


def check_capability(evidence):
    """Reject unknown versions, missing lock support and extra/unknown fields.

    Evidence must originate from the fixed root-owned adapter. Checking this
    reply outside its transaction lock conveys NO permission to mutate a host.
    """
    require(type(evidence) is dict, "capability evidence must be an object")
    require(
        evidence.keys()
        == {
            "protocol",
            "capabilities",
            "lock_wait_seconds",
            "transaction_seconds",
            "recovery_seconds",
        },
        "unknown or missing capability fields",
    )
    require(evidence["protocol"] == VERSION, "unsupported host capability version")
    capabilities = evidence["capabilities"]
    require(
        type(capabilities) is list and all(type(c) is str for c in capabilities),
        "invalid host capabilities",
    )
    require(
        len(capabilities) == len(CAPABILITIES)
        and frozenset(capabilities) == CAPABILITIES,
        "unknown or missing host capability",
    )
    for key, expected in (
        ("lock_wait_seconds", LOCK_WAIT_SECONDS),
        ("transaction_seconds", TRANSACTION_SECONDS),
        ("recovery_seconds", RECOVERY_SECONDS),
    ):
        require(
            type(evidence[key]) is int and evidence[key] == expected,
            "unsupported host timing control",
        )
