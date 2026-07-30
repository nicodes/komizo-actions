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
"""
import os
import sys

try:
    import yaml
except ImportError:
    print("::error::PyYAML is required to read komizo.yml.", file=sys.stderr)
    sys.exit(1)


def fail(msg):
    print(f"::error::{msg}", file=sys.stderr)
    sys.exit(1)


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

    hosts = data.get("hostnames") or []
    if not isinstance(hosts, list):
        fail(f"{cfg}: 'hostnames' must be a list.")

    lines = []
    for i, h in enumerate(hosts):
        if isinstance(h, str):
            name, container, tls = h, None, None
        elif isinstance(h, dict):
            name, container, tls = h.get("name"), h.get("container"), h.get("tls")
        else:
            fail(f"{cfg}: hostnames[{i}] must be a name or a mapping.")
        if not isinstance(name, str) or not name:
            fail(f"{cfg}: hostnames[{i}] has no name.")
        line = name
        if container:
            line += " -> " + str(container)
        if tls:
            line += " " + str(tls)
        lines.append(line)

    with open(out, "w") as f:
        if lines:
            f.write("\n".join(lines) + "\n")

    # The compose path, resolved relative to komizo.yml, for the shell to pick
    # up and confine to the workspace like any other named file.
    print(os.path.normpath(os.path.join(os.path.dirname(cfg) or ".", compose)))


if __name__ == "__main__":
    main()
