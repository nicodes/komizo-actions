#!/usr/bin/env python3
"""Read a komizo.yml deploy manifest and emit the flat `hostnames` file the host
already reads, printing the resolved compose path to stdout.

    parse-config.py <komizo.yml> <out-hostnames-file>

komizo.yml is the app's single, structured deploy config; the host never sees
it. This turns it into the same simple `name [-> container] [tls]` lines the
deploy script has always parsed, so nothing on the box has to learn YAML. The
structured file is an author-time convenience; what ships in the config image is
the format the host validates.

Shape:

    compose: compose.yml          # path, relative to this file (default compose.yml)
    hostnames:
      - app.example.com           # a bare name
      - name: api.example.com
        container: api            # DISPLAY label only (monitor attribution)
      - name: "*.preview.example.com"
        container: api
        tls: on-demand            # only a wildcard needs this

EVERY VALUE IS VALIDATED HERE. It used to be interpolated straight into the
output with str(), which had two silent failure modes, both of them the kind
this toolkit refuses everywhere else:

  * `tls: yes` is a YAML BOOLEAN, so the line became "name -> api True" and the
    host rejected it with a message about arrows that named nothing the author
    had written.
  * a newline in any value emitted TWO lines, the second of which was a hostname
    claim nobody made -- and the shell validator downstream reads the derived
    file line by line, so it would have accepted it as well-formed.

The charsets below are the ones the host enforces (alpine.sh). Checking them
here as well is the same defence-in-depth the rest of this repository uses: the
host is the side that has to be right, and finding out in CI is cheaper than
finding out from a deploy that reverted.
"""
import os
import re
import sys

try:
    import yaml
except ImportError:
    # Deliberately no pip install fallback. This step holds the deploy key and
    # the registry token, and reaching out to PyPI -- or escalating to sudo
    # apt-get -- to fetch an unpinned package is the least verified thing that
    # could happen in it. Every GitHub-hosted runner ships PyYAML; a runner that
    # does not should install it once, on purpose, rather than have this do it
    # silently on every deploy.
    print(
        "::error::PyYAML is required to read komizo.yml. GitHub-hosted runners "
        "ship it; on a self-hosted runner install python3-yaml (or add a "
        "pip install step to your workflow). Alternatively use the "
        "config-compose:/config-hostnames: inputs, which need no YAML parser.",
        file=sys.stderr,
    )
    sys.exit(1)


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

    YAML types are checked before the charset, because the message differs and
    the type error is the one nobody expects: `tls: yes` and `container: 3` are
    both perfectly valid YAML that produce something that is not a string.
    """
    if isinstance(value, bool):
        # The one that actually happened. `tls: yes`, `tls: on` and `tls: true`
        # are all booleans to a YAML parser.
        fail(
            f"{cfg}: hostnames[{i}] '{field}' is the YAML boolean "
            f"{str(value).lower()}, not text. Quote it: {field}: \"...\"."
        )
    if not isinstance(value, str):
        fail(
            f"{cfg}: hostnames[{i}] '{field}' must be text, got "
            f"{type(value).__name__}."
        )
    return value


def main():
    if len(sys.argv) != 3:
        fail("parse-config.py takes <komizo.yml> <out-hostnames-file>.")
    cfg, out = sys.argv[1], sys.argv[2]

    try:
        with open(cfg) as f:
            data = yaml.safe_load(f) or {}
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

    # The compose path, resolved relative to komizo.yml, for the shell to pick
    # up and confine to the workspace like any other named file.
    print(os.path.normpath(os.path.join(os.path.dirname(cfg) or ".", compose)))


if __name__ == "__main__":
    main()
