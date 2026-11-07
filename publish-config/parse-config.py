#!/usr/bin/env python3
"""Read authored komizo.toml and emit the existing flat hostnames protocol.

Usage: parse-config.py <komizo.toml> <out-hostnames-file>

    compose = "compose.yml"
    hostnames = ["app.example.com", {name = "api.example.com", container = "api"},
                 {name = "*.preview.example.com", container = "api", tls = "on-demand"}]

The host receives Compose YAML and validated flat hostname lines. Source values
must preserve each native field's type and cannot inject extra output lines.
"""
import os
import re
import sys

import tomllib
from pathlib import Path


# A hostname, optionally with a single leading "*." wildcard -- the only form
# Caddy accepts, and the same rule the host applies.
HOSTNAME = re.compile(r"^\*\.(?![^.]*\*)[A-Za-z0-9.-]+$|^[A-Za-z0-9.-]+$")
# A compose service name.
CONTAINER = re.compile(r"^[A-Za-z0-9_-]+$")
# How a name's certificate is obtained. SHAPE only: which of these a server can
# actually serve is the host's business, and it refuses the ones it cannot with
# a message naming the file. Deciding that here would put the answer in the
# wrong place and date it to whenever this action was last released.
TLS_MODES = ("on-demand", "dns", "passthrough")


def fail(msg):
    print(f"::error::{msg}", file=sys.stderr)
    sys.exit(1)


def scalar(cfg, i, field, value):
    """One field of one hostname entry, as a string, or fail saying why.

    Types are checked before charsets so a boolean or numeric field produces
    the appropriate diagnostic instead of being coerced into native text.
    """
    if isinstance(value, bool):
        # Native text fields never stringify source booleans.
        fail(
            f"{cfg}: hostnames[{i}] '{field}' is the TOML boolean "
            f"{str(value).lower()}, not text. Quote it: {field} = \"...\"."
        )
    if not isinstance(value, str):
        fail(
            f"{cfg}: hostnames[{i}] '{field}' must be text, got "
            f"{type(value).__name__}."
        )
    return value


def main():
    if len(sys.argv) != 3:
        fail("parse-config.py takes <komizo.toml> <out-hostnames-file>.")
    cfg, out = sys.argv[1], sys.argv[2]

    try:
        path = Path(cfg)
        if (path.suffix != '.toml' or path.is_symlink() or any(parent.is_symlink() for parent in path.parents)
                or not path.is_file() or path.stat().st_size > 65536):
            fail(f'{cfg} must be a bounded regular TOML source file.')
        data = tomllib.loads(path.read_text())
    except Exception as e:  # noqa: BLE001 - surface any parse error verbatim
        fail(f"could not parse {cfg}: {e}")

    if not isinstance(data, dict):
        fail(f"{cfg} must be a mapping with 'compose' and 'hostnames'.")

    compose = data.get("compose", "compose.yml")
    if not isinstance(compose, str) or not compose:
        fail(f"{cfg}: 'compose' must be a path string.")
    # Printed to stdout for the shell to capture, which reads one line. A
    # newline here would make the rest of the path vanish into a second line
    # nothing reads. The shell confines the result to the workspace either way;
    # this is so the failure names the manifest rather than a truncated path.
    if "\n" in compose or "\r" in compose:
        fail(f"{cfg}: 'compose' must not contain a newline.")

    hosts = data.get("hostnames") or []
    if not isinstance(hosts, list):
        fail(f"{cfg}: 'hostnames' must be a list.")

    lines = []
    seen = set()
    for i, h in enumerate(hosts):
        container = tls = None
        if isinstance(h, str) or isinstance(h, bool):
            name = scalar(cfg, i, "name", h)
        elif isinstance(h, dict):
            if "name" not in h:
                fail(f"{cfg}: hostnames[{i}] has no name.")
            name = scalar(cfg, i, "name", h["name"])
            if h.get("container") is not None:
                container = scalar(cfg, i, "container", h["container"])
            if h.get("tls") is not None:
                tls = scalar(cfg, i, "tls", h["tls"])
            extra = set(h) - {"name", "container", "tls"}
            if extra:
                # A typo in a key is otherwise silent: the entry publishes, and
                # whatever the key was meant to do simply does not happen.
                fail(
                    f"{cfg}: hostnames[{i}] has unknown key(s) "
                    f"{', '.join(sorted(extra))}. Expected: name, container, tls."
                )
        else:
            fail(f"{cfg}: hostnames[{i}] must be a name or a mapping.")

        if not HOSTNAME.match(name):
            fail(
                f"{cfg}: hostnames[{i}] '{name}' is not a valid hostname. "
                "Letters, digits, dot and hyphen, with an optional leading '*.'."
            )
        # A name claimed twice on one box is two site blocks for one name, which
        # Caddy rejects outright -- taking down every app, not just this one. The
        # host catches it across apps; within one manifest it is a typo, and this
        # is where it is cheapest to say so.
        if name in seen:
            fail(f"{cfg}: '{name}' is listed twice.")
        seen.add(name)

        if container is not None and not CONTAINER.match(container):
            fail(
                f"{cfg}: hostnames[{i}] container '{container}' is not a compose "
                "service name (letters, digits, underscore, hyphen)."
            )
        if tls is not None and tls not in TLS_MODES:
            fail(
                f"{cfg}: hostnames[{i}] tls '{tls}' is not one of: "
                f"{' '.join(TLS_MODES)}."
            )

        line = name
        if container:
            line += " -> " + container
        if tls:
            line += " " + tls
        lines.append(line)

    with open(out, "w") as f:
        if lines:
            f.write("\n".join(lines) + "\n")

    # The compose path, resolved relative to komizo.toml, for the shell to pick
    # up and confine to the workspace like any other named file.
    print(os.path.normpath(os.path.join(os.path.dirname(cfg) or ".", compose)))


if __name__ == "__main__":
    main()
