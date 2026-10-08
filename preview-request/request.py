#!/usr/bin/env python3
"""preview-request/request.py - decide whether a workflow run is a preview request.

Previews are opt-in. A pull request gets a preview when a trusted person asks
for one with a `/preview` comment, keeps it across pushes while it carries the
preview label, and loses it on `/preview down` or when the PR closes. This
script is the one place that decision is made, so every product's pr-preview
workflow carries the same rule and no product has to carry a script of its own.

It is a pure function of the event payload (plus one PR lookup on comments),
needs no deploy key, no registry token and no server, and runs before any of
those enters the workflow: the jobs that hold secrets are gated on its
outputs, so a request this script refuses never brings a secret into scope.

THE RULE, event by event:

  pull_request
    closed                 -> down, always. Teardown is idempotent on the host,
                              so it runs whether or not the label is present:
                              a label that went missing must not strand a
                              stack.
    synchronize, reopened  -> up only if the PR currently carries the label.
                              The label is the "a preview is up" signal, read
                              from the event payload, so a push to a PR nobody
                              asked a preview for costs nothing.
    opened, ready_for_review, anything else
                           -> nothing. Opening a PR does not deploy it.

  issue_comment (created)
    exact body `/preview`      -> up
    exact body `/preview down` -> down
    anything else, a comment on a plain issue, or a commenter whose
    author_association is not OWNER, MEMBER or COLLABORATOR -> nothing.
    The PR is fetched (the comment payload carries no head sha) and the
    same guards as below apply.

  anything else -> nothing.

GUARDS on every up: the PR's head lives in this repository (never a fork),
the PR's author is OWNER, MEMBER or COLLABORATOR and not dependabot (a
dependabot run is served the Dependabot secret store, so the deploy key
would arrive empty), the PR is open and not a draft. Down requires only the
same-repository head: a fork PR never had a preview to remove.

VALIDATION: the PR number must be a positive integer and the head sha forty
lowercase hex characters, or the step fails rather than passing a malformed
identity on to the jobs that name a host-side stack by it.

The label itself is written by the workflow after a successful up and removed
after down; this script only reads it.
"""

import json
import os
import subprocess
import sys

TRUSTED = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
COMMANDS = {"/preview": "up", "/preview down": "down"}
DEPENDABOT = "dependabot[bot]"
HEX = frozenset("0123456789abcdef")


class Refused(Exception):
    """Not a preview request. The message is the human-readable reason."""


def label_names(pr):
    return {entry.get("name") for entry in pr.get("labels") or [] if isinstance(entry, dict)}


def fetch_pr(repository, number):
    """One read of the PR, needed on comments: the payload has no head sha."""
    out = subprocess.run(
        ["gh", "api", f"repos/{repository}/pulls/{number}"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    return json.loads(out)


def resolve(event, event_name, repository, label, fetch=None):
    """Return {enabled, action, pr, sha, head_ref, reason} or raise Refused."""
    fetch = fetch or fetch_pr
    if event_name == "pull_request":
        pr = event.get("pull_request") or {}
        what = event.get("action")
        if what == "closed":
            action, why = "down", "pull request closed"
        elif what in {"synchronize", "reopened"}:
            if label not in label_names(pr):
                raise Refused(f"{what} without the {label} label")
            action, why = "up", f"{what} with the {label} label"
        else:
            raise Refused(f"{what or 'unknown'} event ignored; comment /preview to deploy")
    elif event_name == "issue_comment":
        if event.get("action") != "created":
            raise Refused(f"comment {event.get('action') or 'unknown'} event ignored")
        issue = event.get("issue") or {}
        comment = event.get("comment") or {}
        if not issue.get("pull_request"):
            raise Refused("comment on an issue, not a pull request")
        body = (comment.get("body") or "").strip()
        if body not in COMMANDS:
            raise Refused("comment is not a preview command")
        association = comment.get("author_association")
        if association not in TRUSTED:
            raise Refused(f"comment by {association or 'unknown'} ignored")
        number = issue.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ValueError("invalid pull request number in the comment payload")
        pr = fetch(repository, number)
        action = COMMANDS[body]
        why = f"comment {body} by {association}"
    else:
        raise Refused(f"{event_name or 'unknown'} is not a preview event")

    head = pr.get("head") or {}
    if (head.get("repo") or {}).get("full_name") != repository:
        raise Refused("head is not in this repository")
    if action == "up":
        if (pr.get("user") or {}).get("login") == DEPENDABOT:
            raise Refused("dependabot pull requests are never previewed")
        if pr.get("author_association") not in TRUSTED:
            raise Refused(f"pull request author is {pr.get('author_association') or 'unknown'}")
        if pr.get("draft"):
            raise Refused("draft pull requests are not previewed")
        if pr.get("state") != "open":
            raise Refused(f"pull request is {pr.get('state') or 'unknown'}, not open")

    number = pr.get("number")
    sha = head.get("sha")
    ref = head.get("ref")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise ValueError("invalid pull request number")
    if not isinstance(sha, str) or len(sha) != 40 or not set(sha) <= HEX:
        raise ValueError("invalid head sha")
    if not isinstance(ref, str) or not ref or any(c in ref for c in "\n\r"):
        raise ValueError("invalid head ref")
    return {
        "enabled": "true",
        "action": action,
        "pr": str(number),
        "sha": sha,
        "head_ref": ref,
        "reason": why,
    }


def main():
    label = os.environ.get("LABEL", "").strip()
    if not label or any(c in label for c in " \n\r,"):
        print("::error::label must be a single non-empty word", file=sys.stderr)
        return 1
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as source:
        event = json.load(source)
    try:
        result = resolve(
            event, os.environ.get("GITHUB_EVENT_NAME", ""), os.environ["GITHUB_REPOSITORY"], label
        )
    except Refused as refused:
        result = {"enabled": "false", "action": "", "pr": "", "sha": "", "head_ref": "", "reason": str(refused)}
    except subprocess.CalledProcessError as error:
        print(f"::error::pull request lookup failed: {error.stderr.strip()}", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        for key, value in result.items():
            # One line per output: a reason is built from payload strings,
            # so it is flattened rather than trusted to be newline-free.
            output.write(f"{key}={' '.join(str(value).split())}\n")
    verb = "preview request" if result["enabled"] == "true" else "no preview request"
    print(f"{verb}: {result['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
