import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import utils  # noqa: E402


MILES_SHA = "af4413b74f33f072883b6fbc760cccba3d56160d"
JOB_ID = 117741957893


def _payload() -> dict:
    """The shape of the dashboard's api/miles/cause-fix.json, trimmed."""
    return {
        "available": True,
        "rows": [
            {
                "id": "test:tests/e2e/ft/test_kill_train.py#cde26034",
                "state": "failing",
                "kind": "test",
                "error": "AssertionError: main_param is None",
                "tests": [{
                    "file": "tests/e2e/ft/test_kill_train.py",
                    "status": "failed",
                    "error": "AssertionError: main_param is None",
                    "exit_code": 1,
                }],
                "jobs": [{
                    "id": JOB_ID,
                    "image": "rocm/sgl-dev:miles-rocm10-mi35x-20261015",
                    "commits": {"radixark/miles": MILES_SHA, "sgl-project/sglang": "7b6993a310"},
                    "refs": {"radixark/miles": "main", "sgl-project/sglang": "sglang-miles"},
                }],
                "frames": [{
                    "repo": "radixark/miles",
                    "path": "miles/backends/megatron_utils/model.py",
                    "line": 120,
                    "func": "train_one_step",
                    "code": "loss = step(batch)",
                }],
                "since": [{
                    "label": "MI35X",
                    "first_failed": {"date": "2026-10-15", "job_url": "https://example.test/job/1",
                                     "image": "rocm/sgl-dev:miles-rocm10-mi35x-20261015"},
                    "last_passed": {"date": "2026-10-14", "job_url": "https://example.test/job/0",
                                    "image": "rocm/sgl-dev:miles-rocm10-mi35x-20261014"},
                    "failed_runs": 4,
                    "lower_bound": False,
                    "windows": [{
                        "repo": "radixark/miles",
                        "base": "a87843494f4083b8be6d575edabf7e0f7ad6f5bc",
                        "head": MILES_SHA,
                        "commits": 81,
                        "url": "https://github.com/radixark/miles/compare/a87843494f40...af4413b74f33",
                        "ref": "main",
                    }],
                }],
                "cause_prs": [{
                    "repo": "radixark/miles", "number": 3423,
                    "sha": "dd1545c31fbb29fc2e70edd86c73ff2e03ddbe12",
                    "url": "https://github.com/radixark/miles/pull/3423",
                    "commit_url": "https://github.com/radixark/miles/commit/dd1545c31fbb",
                    "title": "Keep the weight version with the Megatron model state",
                    "reason": "changed train_one_step() in model.py, on the traceback",
                    "strong": True, "pr_state": "merged",
                }],
                "fix_prs": [],
            },
            {
                "id": "test:tests/other.py#00000000",
                "state": "recovered",
                "kind": "test",
                "error": "x",
                "tests": [],
                "jobs": [{"id": 1}],
                "since": [],
            },
        ],
    }


class MilesWorkflowTests(unittest.TestCase):
    def test_miles_workflows_are_recognised_by_file_name(self):
        self.assertTrue(utils.is_miles_workflow("nightly-test-amd-miles-rocm10.yml"))
        self.assertTrue(utils.is_miles_workflow(
            ".github/workflows/release-docker-amd-miles-rocm724-nightly.yml"
        ))
        self.assertFalse(utils.is_miles_workflow("nightly-test-amd.yml"))

    def test_rows_and_image_of_a_job(self):
        rows = utils.miles_rows_for_job(_payload(), JOB_ID)
        self.assertEqual([r["state"] for r in rows], ["failing"])
        image = utils.miles_job_image(rows, JOB_ID)
        self.assertEqual(image["image"], "rocm/sgl-dev:miles-rocm10-mi35x-20261015")
        self.assertEqual(image["commits"]["radixark/miles"], MILES_SHA)
        self.assertEqual(image["refs"]["sgl-project/sglang"], "sglang-miles")
        self.assertEqual(image["forks"], {})
        self.assertEqual(utils.miles_rows_for_job(None, JOB_ID), [])
        self.assertEqual(utils.miles_job_image([], JOB_ID)["commits"], {})

    def test_a_fork_built_image_in_either_payload_shape(self):
        # The dashboard files a fork's commit under radixark/miles and names
        # the fork; payloads from before that keyed the commit by the fork.
        current = {"id": JOB_ID, "commits": {"radixark/miles": MILES_SHA},
                   "refs": {"radixark/miles": "amd/x"},
                   "forks": {"radixark/miles": "michaelzhang-ai/miles"}}
        older = {"id": JOB_ID, "commits": {"michaelzhang-ai/miles": MILES_SHA},
                 "refs": {"michaelzhang-ai/miles": "amd/x"}}
        for job in (current, older):
            image = utils.miles_job_image([{"jobs": [job]}], JOB_ID)
            self.assertEqual(image["commits"], {"radixark/miles": MILES_SHA})
            self.assertEqual(image["refs"], {"radixark/miles": "amd/x"})
            self.assertEqual(image["forks"], {"radixark/miles": "michaelzhang-ai/miles"})


class EvidenceTests(unittest.TestCase):
    JOB = {"id": JOB_ID, "name": "test-miles-mi35x (nightly-stage-c-8-gpu-mi350)",
           "html_url": "https://example.test/job"}

    def test_evidence_names_the_code_window_and_candidates(self):
        payload = _payload()
        rows = utils.miles_rows_for_job(payload, JOB_ID)
        text = utils.render_miles_evidence(
            self.JOB, rows, utils.miles_job_image(rows, JOB_ID),
            {"miles_src": ".ci-context/miles/src", "sglang_src": "."}, payload,
        )
        self.assertIn("rocm/sgl-dev:miles-rocm10-mi35x-20261015", text)
        self.assertIn(f"https://github.com/radixark/miles/commit/{MILES_SHA}", text)
        self.assertIn("checked out at `.ci-context/miles/src`", text)
        self.assertIn("(branch `sglang-miles`) — checked out at `.`", text)
        self.assertIn("`radixark/miles:miles/backends/megatron_utils/model.py:120` in `train_one_step`", text)
        self.assertIn("failing since [2026-10-15](https://example.test/job/1)", text)
        self.assertIn("81 commit(s)", text)
        self.assertIn("[radixark/miles#3423](https://github.com/radixark/miles/pull/3423)", text)
        self.assertIn("(strong, merged): changed train_one_step()", text)
        self.assertNotIn("other.py", text)  # another job's row

    def test_evidence_flags_a_fork_built_image(self):
        payload = _payload()
        row = payload["rows"][0]
        row["jobs"][0]["refs"]["radixark/miles"] = "amd/lazy-lora-build-test"
        row["jobs"][0]["forks"] = {"radixark/miles": "michaelzhang-ai/miles"}
        row["since"][0]["windows"][0]["head_fork"] = "michaelzhang-ai/miles"
        rows = utils.miles_rows_for_job(payload, JOB_ID)
        text = utils.render_miles_evidence(
            self.JOB, rows, utils.miles_job_image(rows, JOB_ID),
            {"miles_src": ".ci-context/miles/src", "sglang_src": "."}, payload,
        )
        self.assertIn(
            "(fork `michaelzhang-ai/miles`, branch `amd/lazy-lora-build-test`)", text
        )
        self.assertIn("**Built from the fork `michaelzhang-ai/miles`, not from radixark/miles main**", text)
        self.assertIn("failure built from fork `michaelzhang-ai/miles`, so the range includes", text)

    def test_evidence_says_what_is_missing(self):
        text = utils.render_miles_evidence(
            self.JOB, [], {"image": "", "commits": {}, "refs": {}}, {}, None,
        )
        self.assertIn("Image: unknown", text)
        self.assertIn("radixark/miles: unknown (the dashboard has no radixark/miles commit", text)
        self.assertIn("payload could not be fetched", text)
        text = utils.render_miles_evidence(
            self.JOB, [], {"image": "", "commits": {}, "refs": {}}, {}, {"rows": []},
        )
        self.assertIn("no row for this job yet", text)


class FetchPayloadTests(unittest.TestCase):
    def setUp(self):
        utils._miles_payload.clear()

    def test_payload_is_fetched_once_and_unavailable_is_none(self):
        ok = Mock()
        ok.json.return_value = {"available": True, "rows": []}
        with patch("utils.requests.get", return_value=ok) as get:
            self.assertEqual(utils.fetch_miles_cause_fix("u1"), {"available": True, "rows": []})
            utils.fetch_miles_cause_fix("u1")
        self.assertEqual(get.call_count, 1)

        off = Mock()
        off.json.return_value = {"available": False}
        with patch("utils.requests.get", return_value=off):
            self.assertIsNone(utils.fetch_miles_cause_fix("u2"))
        with patch("utils.requests.get", side_effect=utils.requests.ConnectionError("down")):
            self.assertIsNone(utils.fetch_miles_cause_fix("u3"))


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()


class CheckoutTests(unittest.TestCase):
    """The checkouts against throwaway local repositories."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _repo(self, name: str, commits: int) -> tuple[Path, list[str]]:
        repo = self.root / name
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        shas = []
        for i in range(commits):
            (repo / "f.txt").write_text(str(i))
            _git(repo, "add", "f.txt")
            _git(repo, "commit", "-q", "-m", f"c{i}")
            shas.append(_git(repo, "rev-parse", "HEAD"))
        return repo, shas

    def test_miles_worktree_at_the_image_commit(self):
        repo, shas = self._repo("miles", 3)
        dest = self.root / "wt" / ".ci-context" / "miles" / "src"
        with patch("utils.ensure_miles_repo", return_value=repo):
            self.assertTrue(utils.add_miles_worktree(dest, shas[1]))
        self.assertEqual((dest / "f.txt").read_text(), "1")

    def test_miles_worktree_falls_back_to_the_fork(self):
        upstream, _ = self._repo("upstream", 1)
        fork = self.root / "owner" / "miles"
        fork.parent.mkdir()
        _git(self.root, "clone", "-q", str(upstream), str(fork))
        (fork / "f.txt").write_text("fork only")
        _git(fork, "commit", "-q", "-am", "fork commit")
        fork_sha = _git(fork, "rev-parse", "HEAD")
        _git(fork, "config", "uploadpack.allowAnySHA1InWant", "true")
        cache = self.root / "cache"
        _git(self.root, "clone", "-q", str(upstream), str(cache))
        # Let the "https://github.com/<fork>.git" URL resolve to the local fork.
        _git(cache, "config", f"url.{fork.as_uri()}.insteadOf",
             "https://github.com/owner/miles.git")
        dest = self.root / "wt" / "src"
        with patch("utils.ensure_miles_repo", return_value=cache):
            self.assertTrue(utils.add_miles_worktree(dest, fork_sha, "owner/miles"))
        self.assertEqual((dest / "f.txt").read_text(), "fork only")

    def test_sglang_moves_to_an_abbreviated_commit_on_its_branch(self):
        origin, _ = self._repo("origin", 1)
        _git(origin, "checkout", "-q", "-b", "sglang-miles")
        (origin / "f.txt").write_text("miles branch")
        _git(origin, "commit", "-q", "-am", "on the branch")
        branch_sha = _git(origin, "rev-parse", "HEAD")
        _git(origin, "checkout", "-q", "main")

        clone = self.root / "clone"
        _git(self.root, "clone", "-q", "--single-branch", "--branch", "main",
             origin.as_uri(), str(clone))
        self.assertTrue(utils.checkout_sglang_for_miles(clone, branch_sha[:10], "sglang-miles"))
        self.assertEqual((clone / "f.txt").read_text(), "miles branch")
        self.assertFalse(utils.checkout_sglang_for_miles(clone, "0" * 10, ""))

    def test_sglang_commit_dropped_by_a_force_push_is_fetched_by_sha(self):
        origin, _ = self._repo("origin", 1)
        _git(origin, "config", "uploadpack.allowAnySHA1InWant", "true")
        _git(origin, "checkout", "-q", "-b", "sglang-miles")
        (origin / "f.txt").write_text("before the force push")
        _git(origin, "commit", "-q", "-am", "dropped later")
        dropped = _git(origin, "rev-parse", "HEAD")
        _git(origin, "reset", "-q", "--hard", "main")  # the force push

        clone = self.root / "clone"
        _git(self.root, "clone", "-q", "--single-branch", "--branch", "main",
             origin.as_uri(), str(clone))
        found = Mock(status_code=200)
        found.json.return_value = {"sha": dropped}
        with patch("utils.requests.get", return_value=found) as lookup:
            self.assertTrue(utils.checkout_sglang_for_miles(
                clone, dropped[:10], "sglang-miles", token="t",
            ))
        self.assertIn(f"/commits/{dropped[:10]}", lookup.call_args.args[0])
        self.assertEqual((clone / "f.txt").read_text(), "before the force push")


class PromptTests(unittest.TestCase):
    def test_miles_job_prompt_points_at_the_evidence_and_sources(self):
        job = {"id": JOB_ID, "name": "test-miles-mi35x", "steps": []}
        context = {
            "evidence": ".ci-context/miles/EVIDENCE.md",
            "image": "rocm/sgl-dev:miles-rocm10-mi35x-20261015",
            "miles_src": ".ci-context/miles/src", "miles_commit": MILES_SHA,
            "sglang_src": ".", "sglang_commit": "7b6993a310",
        }
        with (
            patch("utils.prepare_miles_context", return_value=context),
            patch("utils.claude_code_analyze", return_value="### Commit Info") as analyze,
        ):
            utils.analyze_job_with_agent(
                job, "https://example.test/run", Path("."),
                "nightly-test-amd-miles-rocm10.yml", head_sha="abc",
            )
        prompt = analyze.call_args.kwargs["prompt"]
        self.assertIn("Miles evidence: .ci-context/miles/EVIDENCE.md", prompt)
        self.assertIn(f"Miles source: .ci-context/miles/src (radixark/miles @ {MILES_SHA})", prompt)
        self.assertIn("sglang source: current directory (sgl-project/sglang @ 7b6993a310", prompt)

    def test_prompt_names_the_fork(self):
        job = {"id": JOB_ID, "name": "test-miles-mi35x", "steps": []}
        context = {
            "evidence": ".ci-context/miles/EVIDENCE.md", "image": "i",
            "miles_src": ".ci-context/miles/src", "miles_commit": MILES_SHA,
            "miles_fork": "michaelzhang-ai/miles", "sglang_src": "", "sglang_commit": "",
        }
        with (
            patch("utils.prepare_miles_context", return_value=context),
            patch("utils.claude_code_analyze", return_value="x") as analyze,
        ):
            utils.analyze_job_with_agent(
                job, "https://example.test/run", Path("."),
                "nightly-test-amd-miles-rocm10.yml",
            )
        self.assertIn(
            f"(radixark/miles @ {MILES_SHA}, built from the fork michaelzhang-ai/miles)",
            analyze.call_args.kwargs["prompt"],
        )

    def test_a_failed_context_still_runs_the_agent(self):
        job = {"id": JOB_ID, "name": "test-miles-mi35x", "steps": []}
        with (
            patch("utils.prepare_miles_context", side_effect=RuntimeError("boom")),
            patch("utils.claude_code_analyze", return_value="x") as analyze,
        ):
            utils.analyze_job_with_agent(
                job, "https://example.test/run", Path("."),
                "nightly-test-amd-miles-rocm10.yml",
            )
        prompt = analyze.call_args.kwargs["prompt"]
        self.assertIn("Miles evidence: unavailable", prompt)
        self.assertIn("NOT the image's sglang", prompt)

    def test_other_workflows_get_no_miles_lines(self):
        job = {"id": 1, "name": "j", "steps": []}
        with (
            patch("utils.prepare_miles_context") as prepare,
            patch("utils.claude_code_analyze", return_value="x") as analyze,
        ):
            utils.analyze_job_with_agent(job, "https://example.test/run", Path("."), "pr-test-amd.yml")
        prepare.assert_not_called()
        self.assertNotIn("Miles", analyze.call_args.kwargs["prompt"])



class DockerHubTests(unittest.TestCase):
    """What Docker Hub says about a job's image, faked from real answers."""

    HUB = {
        "miles-rocm10-mi35x-20261019": ("2026-10-19T14:30:51Z", "sha256:47d17346146f0000"),
        "miles-rocm10-mi35x-20261018": ("2026-10-18T12:40:00Z", "sha256:aaaa"),
        "miles-rocm10-mi35x-20261015": ("2026-10-15T12:47:43Z", "sha256:bbbb"),
        "miles-rocm724-mi35x-20261015": ("2026-10-15T12:47:43Z", "sha256:d8dc7d2b2b10"),
        "miles-rocm724-mi35x-20261013": ("2026-10-13T13:08:07Z", "sha256:cccc"),
    }

    def setUp(self):
        utils._dockerhub_cache.clear()

    def fake_get(self, url, params=None, timeout=None):
        resp = Mock()
        tags_path = "/namespaces/rocm/repositories/sgl-dev/tags"
        if url.endswith(tags_path):
            prefix = params["name"]
            results = [
                {"name": name, "tag_last_pushed": pushed, "digest": digest}
                for name, (pushed, digest) in sorted(
                    self.HUB.items(), key=lambda kv: kv[1][0], reverse=True
                )
                if prefix in name
            ] + [{"name": prefix, "tag_last_pushed": "2026-10-19T14:31:00Z"}]
            resp.status_code, resp.json.return_value = 200, {"results": results}
            return resp
        tag = url.rsplit("/", 1)[-1]
        if tag in self.HUB:
            pushed, digest = self.HUB[tag]
            resp.status_code = 200
            resp.json.return_value = {"tag_last_pushed": pushed, "digest": digest}
        else:
            resp.status_code = 404
        return resp

    def check(self, workflow, job, image):
        with patch("utils.requests.get", side_effect=self.fake_get):
            return "\n".join(utils.check_miles_image(workflow, job, image))

    def test_track_from_image_workflow_or_build_leg(self):
        track = utils.miles_image_track
        self.assertEqual(track("x.yml", {}, "rocm/sgl-dev:miles-rocm10-mi30x-20261015"), "miles-rocm10-mi30x")
        self.assertEqual(track("nightly-test-amd-miles-rocm10.yml", {}, ""), "miles-rocm10-mi35x")
        self.assertEqual(
            track("release-docker-amd-miles-rocm724-nightly.yml", {"name": "publish (rocm724-mi35x)"}, ""),
            "miles-rocm724-mi35x",
        )
        self.assertEqual(track("other.yml", {"name": "x"}, ""), "")

    def test_a_tag_re_pushed_after_the_job_started(self):
        text = self.check(
            "nightly-test-amd-miles-rocm10.yml",
            {"started_at": "2026-10-19T12:00:00Z"},
            "rocm/sgl-dev:miles-rocm10-mi35x-20261019",
        )
        self.assertIn("pushed 2026-10-19 14:30 UTC, digest `sha256:47d17346146f`", text)
        self.assertIn("**Re-pushed after this job started**", text)

    def test_a_nightly_on_an_older_image_because_the_day_was_not_published(self):
        text = self.check(
            "nightly-test-amd-miles-rocm10.yml",
            {"started_at": "2026-10-17T17:40:00Z"},
            "rocm/sgl-dev:miles-rocm10-mi35x-20261015",
        )
        self.assertNotIn("Re-pushed", text)
        self.assertIn(
            "`rocm/sgl-dev:miles-rocm10-mi35x-20261017` (this run's day): **not on Docker Hub** "
            "— that day's build did not publish, so the run fell back to an older image "
            "(`miles-rocm10-mi35x-20261015`).",
            text,
        )
        # The days between the newest tags with no image are named.
        self.assertIn("**No image published on 2026-10-16, 2026-10-17**", text)
        # Newest dated tags only: the undated alias is left out.
        self.assertIn("Newest `miles-rocm10-mi35x-*` tags: `20261019` (2026-10-19 14:30 UTC), `20261018`", text)

    def test_a_build_that_did_not_publish(self):
        text = self.check(
            "release-docker-amd-miles-rocm724-nightly.yml",
            {"name": "publish (rocm724-mi35x)", "started_at": "2026-10-19T12:26:00Z"},
            "",
        )
        self.assertIn("`rocm/sgl-dev:miles-rocm724-mi35x-20261019` (this build's image): **not on Docker Hub**", text)
        self.assertIn("`20261015` (2026-10-15 12:47 UTC), `20261013`", text)

    def test_docker_hub_down_is_said_not_taken_for_missing(self):
        with patch("utils.requests.get", side_effect=utils.requests.ConnectionError("down")):
            text = "\n".join(utils.check_miles_image(
                "nightly-test-amd-miles-rocm10.yml",
                {"started_at": "2026-10-17T17:40:00Z"},
                "rocm/sgl-dev:miles-rocm10-mi35x-20261015",
            ))
        self.assertNotIn("not on Docker Hub", text)
        self.assertEqual(text.count("Docker Hub did not answer"), 3)

    def test_evidence_has_a_docker_hub_section(self):
        text = utils.render_miles_evidence(
            {"id": 1}, [], {"image": "", "commits": {}, "refs": {}}, {}, {"rows": []},
            ["- `rocm/sgl-dev:x` (this run's day): **not on Docker Hub**"],
        )
        self.assertIn("## Docker Hub\n\n- `rocm/sgl-dev:x`", text)


if __name__ == "__main__":
    unittest.main()
