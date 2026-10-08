"""Every branch of the preview-request decision, against fixture payloads.

The decision is a pure function of the event payload plus one PR lookup, so
each clause of the opt-in rule is pinned here: what deploys, what tears down,
what is ignored, and what fails outright. The PR lookup is a fake; nothing
here touches the network or gh.
"""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("preview_request", ROOT / "preview-request/request.py")
request = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(request)

REPO = "example/product"
SHA = "a" * 40
LABEL = "preview"


def pr(**overrides):
    base = {
        "number": 42,
        "state": "open",
        "draft": False,
        "author_association": "OWNER",
        "user": {"login": "someone"},
        "labels": [],
        "head": {"sha": SHA, "ref": "feature", "repo": {"full_name": REPO}},
    }
    base.update(overrides)
    return base


def labelled(**overrides):
    return pr(labels=[{"name": "bug"}, {"name": LABEL}], **overrides)


def push(action="synchronize", **overrides):
    return {"action": action, "pull_request": pr(**overrides)}


def comment(body="/preview", association="OWNER", on_pr=True, action="created"):
    issue = {"number": 42}
    if on_pr:
        issue["pull_request"] = {"url": "https://api.github.com/repos/example/product/pulls/42"}
    return {
        "action": action,
        "issue": issue,
        "comment": {"body": body, "author_association": association},
    }


class Fetch:
    """Fake PR lookup that records whether it was asked."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, repository, number):
        self.calls.append((repository, number))
        return self.result


def resolve(event, name, fetch=None, label=LABEL):
    return request.resolve(event, name, REPO, label, fetch or Fetch(pr()))


class PullRequestEvents(unittest.TestCase):
    def test_push_without_label_is_not_a_request(self):
        with self.assertRaises(request.Refused) as refused:
            resolve(push(), "pull_request")
        self.assertEqual(str(refused.exception), "synchronize without the preview label")

    def test_push_with_label_deploys(self):
        result = resolve({"action": "synchronize", "pull_request": labelled()}, "pull_request")
        self.assertEqual(result, {
            "enabled": "true", "action": "up", "pr": "42", "sha": SHA,
            "head_ref": "feature", "reason": "synchronize with the preview label",
        })

    def test_reopened_with_label_deploys(self):
        result = resolve({"action": "reopened", "pull_request": labelled()}, "pull_request")
        self.assertEqual(result["action"], "up")

    def test_reopened_without_label_is_not_a_request(self):
        with self.assertRaises(request.Refused):
            resolve(push("reopened"), "pull_request")

    def test_opened_and_ready_for_review_do_nothing_even_with_label(self):
        for action in ["opened", "ready_for_review", "labeled", "edited"]:
            with self.subTest(action=action):
                with self.assertRaises(request.Refused) as refused:
                    resolve({"action": action, "pull_request": labelled()}, "pull_request")
                self.assertIn("comment /preview to deploy", str(refused.exception))

    def test_closed_tears_down_without_the_label(self):
        result = resolve(push("closed", state="closed"), "pull_request")
        self.assertEqual(result["action"], "down")
        self.assertEqual(result["reason"], "pull request closed")
        self.assertEqual(result["pr"], "42")
        self.assertEqual(result["sha"], SHA)

    def test_closed_tears_down_a_draft_and_an_untrusted_authors_pr(self):
        for overrides in [dict(draft=True), dict(author_association="CONTRIBUTOR")]:
            with self.subTest(overrides=overrides):
                result = resolve(push("closed", state="closed", **overrides), "pull_request")
                self.assertEqual(result["action"], "down")

    def test_closed_dependabot_pr_is_ignored(self):
        """A dependabot close runs as dependabot, with the Dependabot secret
        store and no deploy key; nothing was ever deployed for it anyway."""
        event = push("closed", state="closed", user={"login": "dependabot[bot]"}, author_association="CONTRIBUTOR")
        with self.assertRaises(request.Refused) as refused:
            resolve(event, "pull_request")
        self.assertEqual(str(refused.exception), "dependabot pull requests are never previewed")

    def test_closed_fork_pr_is_ignored(self):
        event = push("closed", head={"sha": SHA, "ref": "x", "repo": {"full_name": "fork/product"}})
        with self.assertRaises(request.Refused) as refused:
            resolve(event, "pull_request")
        self.assertEqual(str(refused.exception), "head is not in this repository")

    def test_labelled_push_from_a_fork_never_deploys(self):
        event = {"action": "synchronize", "pull_request": labelled(
            head={"sha": SHA, "ref": "x", "repo": {"full_name": "fork/product"}})}
        with self.assertRaises(request.Refused):
            resolve(event, "pull_request")

    def test_labelled_push_on_a_draft_never_deploys(self):
        with self.assertRaises(request.Refused) as refused:
            resolve({"action": "synchronize", "pull_request": labelled(draft=True)}, "pull_request")
        self.assertEqual(str(refused.exception), "draft pull requests are not previewed")

    def test_labelled_dependabot_push_never_deploys(self):
        event = {"action": "synchronize", "pull_request": labelled(
            user={"login": "dependabot[bot]"}, author_association="CONTRIBUTOR")}
        with self.assertRaises(request.Refused) as refused:
            resolve(event, "pull_request")
        self.assertEqual(str(refused.exception), "dependabot pull requests are never previewed")

    def test_labelled_push_by_an_untrusted_author_never_deploys(self):
        for association in ["CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "NONE", None]:
            with self.subTest(association=association):
                event = {"action": "synchronize", "pull_request": labelled(author_association=association)}
                with self.assertRaises(request.Refused):
                    resolve(event, "pull_request")

    def test_label_name_is_configurable(self):
        event = {"action": "synchronize", "pull_request": pr(labels=[{"name": "deploy-me"}])}
        self.assertEqual(resolve(event, "pull_request", label="deploy-me")["action"], "up")
        with self.assertRaises(request.Refused):
            resolve(event, "pull_request")

    def test_pull_request_events_never_fetch(self):
        fetch = Fetch(pr())
        resolve({"action": "synchronize", "pull_request": labelled()}, "pull_request", fetch)
        self.assertEqual(fetch.calls, [])


class CommentEvents(unittest.TestCase):
    def test_preview_command_deploys_and_fetches_the_pr(self):
        fetch = Fetch(pr())
        result = resolve(comment(), "issue_comment", fetch)
        self.assertEqual(fetch.calls, [(REPO, 42)])
        self.assertEqual(result, {
            "enabled": "true", "action": "up", "pr": "42", "sha": SHA,
            "head_ref": "feature", "reason": "comment /preview by OWNER",
        })

    def test_preview_down_tears_down(self):
        result = resolve(comment("/preview down"), "issue_comment")
        self.assertEqual(result["action"], "down")
        self.assertEqual(result["reason"], "comment /preview down by OWNER")

    def test_commands_tolerate_surrounding_whitespace_only(self):
        self.assertEqual(resolve(comment("  /preview \n"), "issue_comment")["action"], "up")
        for body in ["/preview please", "/previewdown", "/Preview", "preview", "/preview up", "", None, "/preview\ndown"]:
            with self.subTest(body=body):
                with self.assertRaises(request.Refused) as refused:
                    resolve(comment(body), "issue_comment")
                self.assertEqual(str(refused.exception), "comment is not a preview command")

    def test_untrusted_commenter_is_ignored(self):
        for association in ["CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "NONE", "MANNEQUIN", None]:
            with self.subTest(association=association):
                fetch = Fetch(pr())
                with self.assertRaises(request.Refused) as refused:
                    resolve(comment(association=association), "issue_comment", fetch)
                self.assertIn("ignored", str(refused.exception))
                self.assertEqual(fetch.calls, [], "an untrusted comment makes no lookup")

    def test_every_trusted_association_may_command(self):
        for association in ["OWNER", "MEMBER", "COLLABORATOR"]:
            with self.subTest(association=association):
                self.assertEqual(resolve(comment(association=association), "issue_comment")["action"], "up")

    def test_comment_on_a_plain_issue_is_ignored(self):
        fetch = Fetch(pr())
        with self.assertRaises(request.Refused) as refused:
            resolve(comment(on_pr=False), "issue_comment", fetch)
        self.assertEqual(str(refused.exception), "comment on an issue, not a pull request")
        self.assertEqual(fetch.calls, [])

    def test_edited_or_deleted_comments_are_ignored(self):
        for action in ["edited", "deleted"]:
            with self.subTest(action=action):
                with self.assertRaises(request.Refused):
                    resolve(comment(action=action), "issue_comment")

    def test_up_refuses_draft_closed_fork_dependabot_and_untrusted_author(self):
        cases = {
            "draft pull requests are not previewed": pr(draft=True),
            "pull request is closed, not open": pr(state="closed"),
            "head is not in this repository": pr(head={"sha": SHA, "ref": "x", "repo": {"full_name": "fork/product"}}),
            "dependabot pull requests are never previewed": pr(user={"login": "dependabot[bot]"}),
            "pull request author is CONTRIBUTOR": pr(author_association="CONTRIBUTOR"),
        }
        for reason, fetched in cases.items():
            with self.subTest(reason=reason):
                with self.assertRaises(request.Refused) as refused:
                    resolve(comment(), "issue_comment", Fetch(fetched))
                self.assertEqual(str(refused.exception), reason)

    def test_down_on_a_closed_pr_is_allowed_but_not_on_a_fork_or_dependabot_pr(self):
        self.assertEqual(resolve(comment("/preview down"), "issue_comment", Fetch(pr(state="closed")))["action"], "down")
        with self.assertRaises(request.Refused):
            resolve(comment("/preview down"), "issue_comment",
                    Fetch(pr(head={"sha": SHA, "ref": "x", "repo": {"full_name": "fork/product"}})))
        with self.assertRaises(request.Refused):
            resolve(comment("/preview down"), "issue_comment", Fetch(pr(user={"login": "dependabot[bot]"})))

    def test_comment_does_not_need_the_label(self):
        self.assertEqual(resolve(comment(), "issue_comment", Fetch(pr(labels=[])))["action"], "up")

    def test_malformed_issue_number_fails(self):
        for number in [0, -1, "42", None, True]:
            with self.subTest(number=number):
                event = comment()
                event["issue"]["number"] = number
                with self.assertRaises(ValueError):
                    resolve(event, "issue_comment")


class Validation(unittest.TestCase):
    def test_invalid_sha_fails_rather_than_refuses(self):
        for sha in ["A" * 40, "a" * 39, "a" * 41, "", None, "g" * 40, "a" * 39 + " "]:
            with self.subTest(sha=sha):
                event = {"action": "closed", "pull_request": pr(head={"sha": sha, "ref": "x", "repo": {"full_name": REPO}})}
                with self.assertRaises(ValueError):
                    resolve(event, "pull_request")

    def test_invalid_pr_number_fails(self):
        for number in [0, -3, "42", None, 4.2, True]:
            with self.subTest(number=number):
                with self.assertRaises(ValueError):
                    resolve(push("closed", number=number), "pull_request")

    def test_invalid_head_ref_fails(self):
        for ref in ["", None, "a\nb"]:
            with self.subTest(ref=ref):
                event = push("closed", head={"sha": SHA, "ref": ref, "repo": {"full_name": REPO}})
                with self.assertRaises(ValueError):
                    resolve(event, "pull_request")

    def test_other_events_are_not_requests(self):
        for name in ["push", "workflow_dispatch", "pull_request_target", ""]:
            with self.subTest(event=name):
                with self.assertRaises(request.Refused):
                    resolve({"action": "x"}, name)

    def test_missing_payload_sections_refuse_rather_than_crash(self):
        with self.assertRaises(request.Refused):
            resolve({}, "pull_request")
        with self.assertRaises(request.Refused):
            resolve({"action": "created"}, "issue_comment")


class Main(unittest.TestCase):
    """The entry point: env in, GITHUB_OUTPUT out, exit codes."""

    def run_main(self, event, name, label=LABEL, fetched=None):
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "event.json"
            event_path.write_text(json.dumps(event))
            out_path = Path(tmp) / "out"
            out_path.touch()
            env = {
                "GITHUB_EVENT_PATH": str(event_path), "GITHUB_EVENT_NAME": name,
                "GITHUB_REPOSITORY": REPO, "GITHUB_OUTPUT": str(out_path), "LABEL": label,
            }
            with patch.dict(os.environ, env, clear=False), \
                 patch.object(request, "fetch_pr", Fetch(fetched or pr())):
                rc = request.main()
            lines = out_path.read_text().splitlines()
            return rc, dict(line.split("=", 1) for line in lines)

    def test_request_writes_every_output(self):
        rc, outputs = self.run_main({"action": "synchronize", "pull_request": labelled()}, "pull_request")
        self.assertEqual(rc, 0)
        self.assertEqual(outputs, {
            "enabled": "true", "action": "up", "pr": "42", "sha": SHA,
            "head_ref": "feature", "reason": "synchronize with the preview label",
        })

    def test_refusal_is_exit_zero_with_enabled_false_and_empty_identity(self):
        rc, outputs = self.run_main(push(), "pull_request")
        self.assertEqual(rc, 0)
        self.assertEqual(outputs["enabled"], "false")
        self.assertEqual(outputs["action"], "")
        self.assertEqual(outputs["pr"], "")
        self.assertEqual(outputs["sha"], "")
        self.assertEqual(outputs["reason"], "synchronize without the preview label")

    def test_comment_path_uses_the_fetched_pr(self):
        rc, outputs = self.run_main(comment(), "issue_comment")
        self.assertEqual(rc, 0)
        self.assertEqual(outputs["action"], "up")
        self.assertEqual(outputs["sha"], SHA)

    def test_invalid_identity_fails_the_step(self):
        rc, outputs = self.run_main(push("closed", number=0), "pull_request")
        self.assertEqual(rc, 1)
        self.assertEqual(outputs, {}, "nothing is written on failure")

    def test_bad_label_input_fails_the_step(self):
        for label in ["", " ", "two words", "a,b"]:
            with self.subTest(label=label):
                rc, outputs = self.run_main(push(), "pull_request", label=label)
                self.assertEqual(rc, 1)
                self.assertEqual(outputs, {})

    def test_failed_lookup_fails_the_step(self):
        def failing(repository, number):
            raise subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 404")
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "event.json"
            event_path.write_text(json.dumps(comment()))
            out_path = Path(tmp) / "out"
            out_path.touch()
            env = {
                "GITHUB_EVENT_PATH": str(event_path), "GITHUB_EVENT_NAME": "issue_comment",
                "GITHUB_REPOSITORY": REPO, "GITHUB_OUTPUT": str(out_path), "LABEL": LABEL,
            }
            with patch.dict(os.environ, env, clear=False), patch.object(request, "fetch_pr", failing):
                self.assertEqual(request.main(), 1)
            self.assertEqual(out_path.read_text(), "")

    def test_outputs_are_single_lines(self):
        event = comment(association="OWNER")
        event["comment"]["author_association"] = "OWNER"
        rc, outputs = self.run_main(event, "issue_comment")
        self.assertEqual(rc, 0)
        for value in outputs.values():
            self.assertNotIn("\n", value)


if __name__ == "__main__":
    unittest.main()
