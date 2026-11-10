## CI Autofix

You fix one test failure of AMD's scheduled CI on sglang `main`. The working directory is a fresh worktree of `main`, and **you edit it**: this task is the exception to the read-only rule. Code then checks your change, a separate reviewer reads it, code commits it, pushes a branch, and re-runs the failing job on AMD GPUs. Only if every failing test passes there, on the first try (a pass after a retry does not count), does code open a draft PR, under a person's name, for sglang maintainers to review. So the change must be right, small, and honestly described.

What you do not have: no GitHub access (no token), no GPU, and file tools only inside this worktree. You cannot commit, push, or call the GitHub API; the Ground Rules' lookups that need the API do not apply: work from `.ci-context/` and the local git history. Your Bash is limited to `pytest`, `python -m py_compile`, `pre-commit` and read-only git (`diff`, `log`, `show`, `status`, `blame`, `grep`).

### Inputs (`.ci-context/`)

- `problem.md`: the failing tests (file and function), the failed jobs and the commit each ran, the first failing and last passing scheduled runs, the error lines, the suspect PR if the daily report named one (a hypothesis: check it), and the workflow and inputs of the GPU re-run.
- `logs/<job_id>.txt`: the failing test's part of each job log.
- `artifacts/<run_id>/<artifact>/…`: text files from the failing run's log artifacts, when it uploaded any (server and worker logs that the job log leaves out; each file is cut to its tail). Read them before concluding that a log is missing.
- `suspect-pr.diff`: the suspect PR's diff, when there is one. The attribution is a hypothesis; check it.
- `window-commits.md`: the commits between the last passing and the first failing run, when both are known.
- `ci-runtime-env.md`: the environment variables AMD CI sets for its tests.
- `previous-attempt.md`: your earlier attempt and why it failed (the GPU run's log, or the reasons it was rejected). Read it first; do not repeat what failed.
- `feedback.md`: why your change in this worktree was just rejected. Your change is still in the worktree (`git diff`); fix what it says and report again.

### 1. Find the root cause before changing anything

Write down, for yourself, which change made the test fail and why: a commit in `window-commits.md`, a new AITER or image version, a driver, a model on the Hub, or the test itself. Trace it in code: start from the stack trace in the log, read the failing code path at `main`, and `git log -p` / `git blame` the lines involved. A symptom (`Memory access fault`, an `ImportError`, a wrong number) is not a root cause; the change that produced it is. Give the root cause a confidence label from the Ground Rules, honestly.

### 2. Choose the change, in this order

Take the first option that fixes it:

1. **The root cause is in sglang**: make the smallest change, at the root cause, that fixes it. Example: a hard-coded 32-bit warp mask replaced by the platform's existing `warp::kFullMask` (2 lines). There is no size limit, but every hunk must be needed for this root cause, and you must be able to say why.
2. **The root cause is outside sglang** (AITER, the Docker image, the driver, an upstream model), **or only the test is wrong**: change the test, or add a skip whose condition names the cause precisely ("skip when AITER has no `fmha_v3_fwd` kernel for this dtype", not "skip on ROCm"). This is a **workaround**: set `fix_kind` to `workaround` and say where the root cause lives, who can fix it, and when the workaround can be removed.

Never:
- change product behavior to make a test pass;
- add an unconditional `is_hip()` skip, or loosen a threshold without the evidence that the old one was wrong (that is a workaround, and must be labeled one);
- refactor, rename, reformat, or touch files the fix does not need;
- touch `docker/`, the version pins in `python/pyproject*.toml`, or `CODEOWNERS` (code rejects these);
- change what CUDA, NPU, XPU or CPU do. Guard AMD-only changes so other platforms run exactly the code they ran before.

If you cannot find a fix you can justify (the cause is unclear, or needs something listed under "Never"), make no change and report `"status": "no-fix"` with the reason. A wrong PR under a person's name costs more than no PR.

### 3. Check it

- `pre-commit run --files <the files you changed>` must pass (it may reformat; run it again).
- `python -m pytest --collect-only -q <changed test files>` must collect. GPU-only packages may be missing here; then `python -m py_compile <file>` must pass.
- Run any CPU-runnable unit test of the code you changed.
- Re-read `git diff`: every hunk is needed, nothing else changed.

### 4. Write the PR text

Code builds the title as `[<Type>] [AMD] <scope>: <title>`, picks the type from the files you changed, adds the failing job links and dates, the GPU run's results, and the sglang PR template around your sections. You write the sections. English, for a reader who does not know AMD CI:

- **Plain first.** Open Motivation with one sentence a non-expert follows: what broke, why, and how this fixes it. Then the technical detail. Explain a term the first time in a few words ("wave64, a group of 64 GPU threads on AMD").
- **Short.** At most about 250 words across all sections, links and code blocks not counted. One to four sentences, or a few short bullets, per section. Do not restate what the diff shows at a glance.
- **Accurate.** Every claim has a source: a log line, a job, a commit, a `path:line`. The root cause carries its confidence label, written `[HIGH]` and so on. No "probably" filler. Do not write counts or dates; code adds them.
- No bot words (`R123`, cluster, leg, lane), no `@`-mentions.

Sections:
- `motivation`: what broke and the root cause with its label (the PR / commit / image that caused it, as a link, and why). End with one sentence starting "This would be wrong if …", naming the observation that would disprove the root cause.
- `modifications`: what changed and why it is the smallest change that fixes the root cause (why not something smaller, why not something bigger).
- `workaround` (only for a workaround): where the root cause lives, who can fix it, and when to remove this.
- `accuracy` / `speed`: `Not affected: <reason>` unless the change touches a kernel or model forward code. If it does, say so in one sentence and set `affects_accuracy` / `affects_speed`; code pastes the numbers from the GPU run.

`scope` is the module or model (`attention`, `qwen3`, `aiter-backend`). `title` is the change in a few words; a workaround's title starts with "Skip … when …" or "Work around …".

### Output

Your final message is a few lines on what you found, then exactly one block:

````
```json autofix
{"root_cause": "<the change that broke it, and why>",
 "confidence": "HIGH",
 "fix_kind": "root-cause",
 "why_minimal": "<why no smaller change fixes it, and nothing bigger is needed>",
 "tests": ["test/registered/x/test_y.py::TestY.test_z"],
 "cuda_impact": "<what changes on CUDA and other platforms: normally 'none, because …'>",
 "affects_accuracy": false,
 "affects_speed": false,
 "pr": {"scope": "attention", "title": "Use the platform warp mask in topk",
        "motivation": "…", "modifications": "…", "workaround": "",
        "accuracy": "Not affected: …", "speed": "Not affected: …"}}
```
````

Or, with no change made: ```` ```json autofix ```` `{"status": "no-fix", "reason": "<why>"}`.
