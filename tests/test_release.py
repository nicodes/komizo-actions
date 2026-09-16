"""Real Git source artifacts and deterministic GitHub API fixtures; no network."""

import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "release", Path(__file__).resolve().parents[1] / "scripts/release.py"
)
r = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(r)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.oldcwd = Path.cwd()
        self.addCleanup(os.chdir, self.oldcwd)
        self.remote = root / "remote.git"
        r.git("init", "--bare", str(self.remote))
        self.work = root / "work"
        r.git("init", "-b", "main", str(self.work))
        os.chdir(self.work)
        r.git("config", "user.name", "Release Test")
        r.git("config", "user.email", "release@example.invalid")
        r.git("remote", "add", "origin", str(self.remote))
        for name in sorted(r.SUBACTIONS):
            Path(name).mkdir()
            Path(name, "action.yml").write_text(f"name: {name}\n")
        Path("deploy").mkdir()
        Path("deploy/action.yml").write_text(
            "\n".join(
                f"- uses: {r.REPO}/{name}@v0.0.1" for name in sorted(r.SUBACTIONS)
            )
            + "\n"
        )
        r.git("add", ".")
        r.git("commit", "-m", "source")
        self.source = r.git("rev-parse", "HEAD")
        r.git("push", "origin", "main")
        self.version = "v1.2.3"
        self.release_record = None
        self.created = 0
        self.fixture = {}

    def prepare(self, push=False):
        with contextlib.redirect_stdout(io.StringIO()):
            r.prepare(self.version, self.source, push)
        return r.git("rev-parse", f"release/{self.version}")

    def merged(self, mode="squash"):
        head = self.prepare(True)
        tree = r.git("rev-parse", f"{head}^{{tree}}")
        parents = ["-p", self.source]
        if mode == "merge":
            parents += ["-p", head]
        merged = r.git("commit-tree", tree, *parents, input=f"{mode} candidate\n")
        r.git("update-ref", "refs/heads/main", merged)
        r.git("reset", "--hard", merged)
        r.git("push", "origin", "main")
        self.sha = merged
        self.head = head
        self.fixture = {
            "pulls/7": {
                "merged": True,
                "state": "closed",
                "merge_commit_sha": merged,
                "base": {"repo": {"full_name": r.REPO}, "ref": "main"},
                "head": {
                    "repo": {"full_name": r.REPO},
                    "ref": f"release/{self.version}",
                    "sha": head,
                },
            },
            f"actions/workflows/{r.WORKFLOW_ID}": {
                "id": r.WORKFLOW_ID,
                "path": ".github/workflows/ci.yml",
            },
            f"actions/workflows/{r.WORKFLOW_ID}/runs": {"workflow_runs": [{"id": 10}]},
            "actions/runs/10": {
                "workflow_id": r.WORKFLOW_ID,
                "path": ".github/workflows/ci.yml",
                "repository": {"full_name": r.REPO},
                "head_repository": {"full_name": r.REPO},
                "event": "push",
                "head_branch": "main",
                "head_sha": merged,
                "status": "completed",
                "conclusion": "success",
                "check_suite_id": 20,
                "run_attempt": 1,
            },
            "check-suites/20": {
                "app": {"id": r.APP_ID},
                "head_sha": merged,
                "head_branch": "main",
                "status": "completed",
                "conclusion": "success",
            },
            "actions/runs/10/attempts/1/jobs": {
                "jobs": [
                    {
                        "name": "ci",
                        "status": "completed",
                        "conclusion": "success",
                        "head_sha": merged,
                        "run_id": 10,
                        "check_run_url": "https://api.github.com/check/30",
                    }
                ]
            },
            "check-suites/20/check-runs": {
                "check_runs": [
                    {
                        "name": "ci",
                        "status": "completed",
                        "conclusion": "success",
                        "head_sha": merged,
                        "url": "https://api.github.com/check/30",
                        "app": {"id": r.APP_ID},
                        "check_suite": {"id": 20},
                    }
                ]
            },
        }
        return merged

    def api(self, path, optional=False):
        if path == f"releases/tags/{self.version}":
            return self.release_record
        return copy.deepcopy(self.fixture[path.split("?")[0]])

    def validate(self):
        with patch.object(r, "api", self.api), contextlib.redirect_stdout(
            io.StringIO()
        ):
            r.validate(self.version, 7, self.sha)

    def publish(self, lost_response=False):
        real_run = r.run

        def fake_run(*args, **kwargs):
            if args[:3] == ("gh", "release", "create"):
                self.assertIn("--verify-tag", args)
                self.assertTrue(r.remote_tag(self.version, self.sha))
                self.created += 1
                self.release_record = {
                    "tag_name": self.version,
                    "draft": False,
                    "prerelease": False,
                }
                return subprocess.CompletedProcess(
                    args,
                    1 if lost_response else 0,
                    "",
                    "lost response" if lost_response else "",
                )
            return real_run(*args, **kwargs)

        with patch.object(r, "api", self.api), patch.object(
            r, "run", fake_run
        ), contextlib.redirect_stdout(io.StringIO()):
            r.publish(self.version, 7, self.sha)

    def test_prepare_is_deterministic_pin_only_and_never_pushes_main(self):
        hook = self.remote / "hooks/pre-receive"
        hook.write_text(
            '#!/bin/sh\nwhile read -r old new ref; do\n if [ "$ref" = refs/heads/main ]; then exit 1; fi\ndone\n'
        )
        hook.chmod(0o755)
        first = self.prepare(True)
        self.assertEqual(first, self.prepare(True))
        self.assertEqual(r.git("rev-parse", "HEAD"), self.source)
        self.assertEqual(
            r.git("diff", "--name-only", self.source, first), "deploy/action.yml"
        )
        self.assertEqual(r.git("tag", "--list"), "")
        self.assertEqual(
            r.git("ls-remote", "origin", "refs/heads/main").split()[0], self.source
        )
        rejection = r.run(
            "git", "push", "origin", f"{first}:refs/heads/main", check=False
        )
        self.assertNotEqual(rejection.returncode, 0)

    def test_prepare_refuses_remote_branch_drift(self):
        r.git("push", "origin", f"{self.source}:refs/heads/release/{self.version}")
        with self.assertRaisesRegex(RuntimeError, "remote candidate branch differs"):
            self.prepare(True)

    def test_prepare_fails_closed_on_fetch_failure(self):
        r.git("remote", "set-url", "origin", str(self.remote / "missing"))
        with self.assertRaises(RuntimeError):
            self.prepare()
        self.assertEqual(r.git("branch", "--list", "release/*"), "")

    def test_prepare_rejects_missing_and_unknown_siblings(self):
        for content in [
            f"- uses: {r.REPO}/unknown@main\n",
            f"- uses: {r.REPO}/connect@main\n",
        ]:
            with self.subTest(content=content):
                Path("deploy/action.yml").write_text(content)
                r.git("add", ".")
                r.git("commit", "-m", "bad source")
                with self.assertRaises(RuntimeError):
                    r.canonical_tree(r.git("rev-parse", "HEAD"))
        r.git("reset", "--hard", self.source)
        r.git("rm", "connect/action.yml")
        r.git("commit", "-m", "missing sibling")
        with self.assertRaisesRegex(RuntimeError, "missing sibling"):
            r.canonical_tree(r.git("rev-parse", "HEAD"))

    def test_all_merge_strategies_use_tree_not_parent_shape(self):
        for mode in ["merge", "squash", "rebase"]:
            with self.subTest(mode=mode):
                # Reuse a fixed candidate; replace main only within this local fixture.
                r.git("reset", "--hard", self.source)
                r.git("push", "--force", "origin", "main")
                self.merged(mode)
                self.validate()

    def test_pr_and_metadata_drift_rejected(self):
        self.merged()
        original = copy.deepcopy(self.fixture)
        mutations = [
            ("merged", False),
            ("state", "open"),
            ("merge_commit_sha", self.source),
        ]
        for key, value in mutations:
            with self.subTest(key=key):
                self.fixture = copy.deepcopy(original)
                self.fixture["pulls/7"][key] = value
                with self.assertRaises(RuntimeError):
                    self.validate()
        for side, key, value in [
            ("base", "ref", "other"),
            ("head", "ref", "other"),
            ("base", "repo", {"full_name": "foreign/repo"}),
            ("head", "repo", {"full_name": "foreign/repo"}),
        ]:
            with self.subTest(side=side, key=key):
                self.fixture = copy.deepcopy(original)
                self.fixture["pulls/7"][side][key] = value
                with self.assertRaises(RuntimeError):
                    self.validate()
        self.fixture = original
        message = r.git("show", "-s", "--format=%B", self.head).replace(
            self.version, "v9.9.9"
        )
        bad = r.git(
            "commit-tree", f"{self.head}^{{tree}}", "-p", self.source, input=message
        )
        r.git("push", "origin", f"{bad}:refs/heads/bad")
        self.fixture["pulls/7"]["head"]["sha"] = bad
        with self.assertRaisesRegex(RuntimeError, "metadata/version"):
            self.validate()

    def test_merged_tree_drift_rejected(self):
        self.merged()
        Path("extra").write_text("not a pin\n")
        r.git("add", ".")
        r.git("commit", "-m", "drift")
        self.sha = r.git("rev-parse", "HEAD")
        r.git("push", "origin", "main")
        self.fixture["pulls/7"]["merge_commit_sha"] = self.sha
        with self.assertRaisesRegex(RuntimeError, "tree drift"):
            self.validate()

    def test_canonical_pin_drift_rejected_even_if_metadata_matches_tree(self):
        self.merged()
        Path("deploy/action.yml").write_text(f"- uses: {r.REPO}/connect@main\n")
        r.git("add", ".")
        tree = r.git("write-tree")
        bad = r.git(
            "commit-tree",
            tree,
            "-p",
            self.source,
            input=r.metadata(self.version, self.source, tree),
        )
        r.git("push", "origin", f"{bad}:refs/heads/bad")
        self.fixture["pulls/7"]["head"]["sha"] = bad
        with self.assertRaisesRegex(RuntimeError, "tree drift"):
            self.validate()

    def test_trusted_ci_rejects_bad_or_missing_evidence(self):
        self.merged()
        original = copy.deepcopy(self.fixture)
        cases = [
            ("actions/runs/10", "event", "pull_request"),
            ("actions/runs/10", "event", "workflow_dispatch"),
            ("actions/runs/10", "head_sha", self.source),
            ("actions/runs/10", "head_branch", "feature"),
            ("actions/runs/10", "workflow_id", 1),
            ("actions/runs/10", "path", ".github/workflows/other.yml"),
            ("actions/runs/10", "repository", {"full_name": "foreign/repo"}),
            ("actions/runs/10", "head_repository", {"full_name": "foreign/repo"}),
            ("actions/runs/10", "status", "queued"),
            ("actions/runs/10", "conclusion", "failure"),
            ("actions/runs/10", "conclusion", None),
            ("check-suites/20", "app", {"id": 1}),
            ("check-suites/20", "conclusion", "failure"),
            (f"actions/workflows/{r.WORKFLOW_ID}/runs", "workflow_runs", []),
            ("actions/runs/10/attempts/1/jobs", "jobs", []),
            ("check-suites/20/check-runs", "check_runs", []),
        ]
        for path, key, value in cases:
            with self.subTest(path=path, key=key, value=value):
                self.fixture = copy.deepcopy(original)
                self.fixture[path][key] = value
                with patch.object(r, "api", self.api), self.assertRaises(RuntimeError):
                    r.trusted_ci(self.sha)
        for path, collection, key, value in [
            ("actions/runs/10/attempts/1/jobs", "jobs", "conclusion", "failure"),
            ("actions/runs/10/attempts/1/jobs", "jobs", "check_run_url", "wrong"),
            ("check-suites/20/check-runs", "check_runs", "app", {"id": 1}),
            ("check-suites/20/check-runs", "check_runs", "check_suite", {"id": 99}),
            ("check-suites/20/check-runs", "check_runs", "head_sha", self.source),
        ]:
            with self.subTest(path=path, key=key):
                self.fixture = copy.deepcopy(original)
                self.fixture[path][collection][0][key] = value
                with patch.object(r, "api", self.api), self.assertRaises(RuntimeError):
                    r.trusted_ci(self.sha)

    def test_publish_new_release_and_completed_retry_is_noop(self):
        self.merged()
        self.publish()
        tag = r.git("rev-parse", f"refs/tags/{self.version}")
        self.release_record["body"] = "operator notes remain unchanged"
        self.publish()
        self.assertEqual(self.created, 1)
        self.assertEqual(self.release_record["body"], "operator notes remain unchanged")
        self.assertEqual(tag, r.git("rev-parse", f"refs/tags/{self.version}"))
        self.assertEqual(r.git("rev-parse", f"{self.version}^{{}}"), self.sha)

    def test_publish_resumes_existing_annotated_tag_and_lost_release_response(self):
        self.merged()
        r.git("tag", "-a", self.version, self.sha, "-m", "existing")
        r.git("push", "origin", self.version)
        self.publish(lost_response=True)
        self.assertEqual(self.created, 1)

    def test_lost_tag_push_response_is_reread(self):
        self.merged()
        real_run = r.run

        def lost(*args, **kwargs):
            result = real_run(*args, **kwargs)
            if args[:3] == ("git", "push", "origin") and "refs/tags/" in args[-1]:
                return subprocess.CompletedProcess(args, 1, "", "lost response")
            return result

        with patch.object(r, "run", lost):
            self.publish()
        self.assertEqual(self.created, 1)

    def test_wrong_or_lightweight_tag_rejected(self):
        self.merged()
        r.git("tag", self.version, self.sha)
        r.git("push", "origin", self.version)
        with self.assertRaisesRegex(RuntimeError, "annotated tag"):
            self.publish()
        self.assertEqual(self.created, 0)

    def test_mismatched_annotated_tag_rejected(self):
        self.merged()
        r.git("tag", "-a", self.version, self.source, "-m", "wrong")
        r.git("push", "origin", self.version)
        with self.assertRaisesRegex(RuntimeError, "annotated tag"):
            self.publish()

    def test_existing_draft_or_prerelease_rejected(self):
        self.merged()
        for key in ["draft", "prerelease"]:
            self.release_record = {
                "tag_name": self.version,
                "draft": False,
                "prerelease": False,
                key: True,
            }
            with self.subTest(key=key), self.assertRaisesRegex(
                RuntimeError, "completed release"
            ):
                self.publish()

    def test_publish_exact_older_merged_commit_not_moving_main(self):
        self.merged()
        Path("later").write_text("later main change\n")
        r.git("add", ".")
        r.git("commit", "-m", "advance main")
        later = r.git("rev-parse", "HEAD")
        r.git("push", "origin", "main")
        r.git("checkout", "--detach", self.sha)
        self.publish()
        self.assertNotEqual(later, r.git("rev-parse", f"{self.version}^{{}}"))
        self.assertEqual(self.sha, r.git("rev-parse", f"{self.version}^{{}}"))

    def test_outside_tag_writer_race_fails_without_overwriting(self):
        self.merged()
        real_run = r.run
        raced = False

        def race(*args, **kwargs):
            nonlocal raced
            if (
                args[:3] == ("git", "push", "origin")
                and "refs/tags/" in args[-1]
                and not raced
            ):
                raced = True
                real_run(
                    "git", "push", "origin", f"{self.source}:refs/tags/{self.version}"
                )
            return real_run(*args, **kwargs)

        with patch.object(r, "run", race), self.assertRaisesRegex(
            RuntimeError, "annotated tag"
        ):
            self.publish()
        self.assertTrue(raced)
        self.assertEqual(self.created, 0)
        self.assertEqual(
            r.git("ls-remote", "origin", f"refs/tags/{self.version}").split()[0],
            self.source,
        )

    def test_publish_remote_tag_network_failure_is_fatal(self):
        self.merged()
        real_run = r.run

        def fail(*args, **kwargs):
            if args[:3] == ("git", "ls-remote", "--tags"):
                raise RuntimeError("remote unavailable")
            return real_run(*args, **kwargs)

        with patch.object(r, "run", fail), self.assertRaisesRegex(
            RuntimeError, "remote unavailable"
        ):
            self.publish()
        self.assertEqual(self.created, 0)

    def test_api_only_explicit_404_means_absent(self):
        for status in ["403", "401", "500", "network unavailable", "404"]:
            result = subprocess.CompletedProcess(
                [], 1, "", f"gh: error (HTTP {status})"
            )
            with self.subTest(status=status), patch.object(
                r, "run", return_value=result
            ):
                if status == "404":
                    self.assertIsNone(r.api("releases/tags/v1.2.3", optional=True))
                else:
                    with self.assertRaises(RuntimeError):
                        r.api("releases/tags/v1.2.3", optional=True)

    def test_failed_release_create_does_not_claim_success(self):
        self.merged()
        real_run = r.run

        def failed(*args, **kwargs):
            if args[:3] == ("gh", "release", "create"):
                return subprocess.CompletedProcess(args, 1, "", "denied")
            return real_run(*args, **kwargs)

        with patch.object(r, "api", self.api), patch.object(
            r, "run", failed
        ), self.assertRaisesRegex(RuntimeError, "did not complete"):
            r.publish(self.version, 7, self.sha)
        self.assertTrue(r.remote_tag(self.version, self.sha))

    def test_tag_fetch_conflict_is_not_force_overwritten(self):
        self.merged()
        r.git("tag", "-a", self.version, self.source, "-m", "remote")
        r.git("push", "origin", self.version)
        remote_id = r.git("rev-parse", self.version)
        r.git("tag", "-d", self.version)
        r.git("tag", "-a", self.version, self.sha, "-m", "local")
        local_id = r.git("rev-parse", self.version)
        with self.assertRaises(RuntimeError):
            self.validate()
        self.assertEqual(r.git("rev-parse", self.version), local_id)
        self.assertIn(
            remote_id, r.git("ls-remote", "origin", f"refs/tags/{self.version}")
        )


class WorkflowContracts(unittest.TestCase):
    def test_manual_stages_permissions_and_exact_sha_test_order(self):
        import yaml

        root = Path(__file__).resolve().parents[1]
        doc = yaml.safe_load((root / ".github/workflows/release.yml").read_text())
        # PyYAML's YAML 1.1 loader interprets the unquoted key `on` as True.
        events = doc.get("on", doc.get(True))
        self.assertEqual(set(events), {"workflow_dispatch"})
        inputs = events["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["stage"]["options"], ["prepare", "publish"])
        self.assertNotIn("bump", inputs)
        self.assertFalse(doc["concurrency"]["cancel-in-progress"])
        job = doc["jobs"]["release"]
        self.assertEqual(job["permissions"]["pull-requests"], "read")
        steps = job["steps"]
        prepare = next(s for s in steps if "--push" in s.get("run", ""))
        self.assertEqual(prepare["if"], "inputs.stage == 'prepare'")
        validation = next(
            i for i, s in enumerate(steps) if " validate " in s.get("run", "")
        )
        testing = next(
            i for i, s in enumerate(steps) if s.get("uses") == "./.github/actions/test"
        )
        publication = next(
            i for i, s in enumerate(steps) if " publish " in s.get("run", "")
        )
        self.assertLess(validation, testing)
        self.assertLess(testing, publication)
        self.assertIn(
            'git checkout --detach "$CANDIDATE_SHA"', steps[validation]["run"]
        )
        for index in [validation, testing, publication]:
            self.assertEqual(steps[index]["if"], "inputs.stage == 'publish'")
        self.assertNotIn(
            "HEAD:main", (root / ".github/workflows/release.yml").read_text()
        )


if __name__ == "__main__":
    unittest.main()
