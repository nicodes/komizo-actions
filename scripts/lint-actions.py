import glob, os, subprocess, sys, tempfile
import yaml

rc = 0
# Both the actions people call and this repository's own composite
# steps: .github/actions/*/action.yml is shell too, and it is the half
# that guards the release.
paths = sorted(glob.glob("*/action.yml") + glob.glob(".github/actions/*/action.yml"))
for path in paths:
    doc = yaml.safe_load(open(path)) or {}
    steps = (doc.get("runs") or {}).get("steps") or []
    for i, step in enumerate(steps):
        if step.get("shell") != "bash" or "run" not in step:
            continue
        with tempfile.NamedTemporaryFile(
            "w", suffix=".sh", delete=False
        ) as f:
            f.write("#!/usr/bin/env bash\n")
            f.write(step["run"])
            name = f.name
        # SC2154 and SC2153: both are artefacts of the same thing --
        # these fragments read values from the step's own env: block,
        # which the extracted file does not carry. SC2154 reports them as
        # "referenced but not assigned"; SC2153 reports an UPPER_CASE env
        # read (URLS, NAMES, SSH_PORT) as a possible misspelling of a
        # lower_case local it resembles. Neither is a bug -- the env
        # block is the assignment shellcheck cannot see.
        r = subprocess.run(
            ["shellcheck", "--shell=bash", "--exclude=SC2154,SC2153", name]
        )
        os.unlink(name)
        if r.returncode != 0:
            label = step.get("name", "unnamed")
            print(f"::error::shellcheck failed: {path} step #{i + 1} ({label})")
            rc = 1
sys.exit(rc)
