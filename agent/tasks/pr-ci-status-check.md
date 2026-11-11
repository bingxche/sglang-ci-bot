## PR CI Status Check

**Purpose.** For **every CI job that executed and failed** on this PR's head commit, decide whether **this PR caused the failure**. A wrong "unrelated" lets a broken PR merge; a wrong "related" sends the author chasing someone else's bug. Attribution is the job; CI completeness and coverage are secondary notes.

### Start from the evidence pack, then investigate freely

The harness has already collected a starting point in `.ci-context/`. Read `evidence.md` first:

- every workflow run for the PR head (the attempt current when the check runs). For each failed job: the failing test file and cases, a log excerpt, **raw metrics** and how **each attempt** failed, the **commit the job actually tested** (often a merge commit, not the PR head), the CI image tag **and digest**, the image's real `ENV`, the GPU, launch configs, backend-selection lines, and AITER kernel modules loaded;
- the same job and test case in recent `main` runs, with whether each run is comparable (same GPU, same image build or image line, same launch config);
- `pr.diff`, full logs in `logs/` and `logs/main/`, and the gaps the harness could not fill;
- `ci-runtime-env.md`: env vars the workflows and container scripts set, read statically from the checkout. The image's actual `ENV` is in each failure's `F….env`.

Every fact has an ID (`D3`, `F1.env`, `F1.M2`, …). The current directory is checked out at the commit CI tested (stated at the top of `evidence.md`).

This is a starting point, not a boundary. Investigate whatever the question needs: read the source, `git log`/`git blame`, download more logs or older `main` runs, read dependency source (e.g. AITER at the commit in the CI image), search issues and PRs. Download a log to a file once, then grep the file.

### Judgment bar

| Verdict | Use it when |
|---|---|
| 🔴 `related` | You can show the chain: the change (diff) → the changed path ran in this job (environment, launch config, backend/kernel lines, or log) → the failure follows from it (log, metrics). |
| 🟡 `possibly_related` | There is concrete reason to suspect the PR — e.g. the failure is new against comparable `main` runs and the changed code plausibly ran — but the chain is incomplete. |
| 🟢 `unrelated` | Only with a positive reason. **Comparable recurrence:** comparable `main` runs fail the same test case in the same mode, and the PR is no worse in severity (raw metrics) or frequency. **Independence:** the failure happens before or outside anything the PR changes, shown by the diff and the log, with any CI, image, build, or dependency changes in the PR addressed explicitly. |
| ⚪ `unknown` | None of the above is supported. Say what would decide it. |

How to weigh evidence:

- **Recurrence on `main` is evidence, not proof.** A flaky `main` failure can become consistent or worse under a PR. Compare raw metrics (accuracy 0.69 and 0.00 are different failures), failure mode, and frequency.
- **Read each attempt on its own.** A retried test can fail differently each time; recurrence explains the failure only if it explains every attempt.
- **Judge env gates against the environment the job ran with**, never against defaults in the code.
- **Reached is not active.** A line logged when an object is constructed shows the code was reached, not that the feature was used. Prefer evidence of use: kernel modules loaded, dispatch or backend selection, changed outputs.
- **Infrastructure-looking failures can be the PR's** when the PR changes images, dependencies, or CI config.
- **A lost runner** (mode `runner_lost`: the runner went away mid-job, no step failed, usually no log) is 🟢 `unrelated` on `independence` when other jobs lost their runner in the same window or the job passes in other attempts: cite the failure's evidence and a diff hunk, and address any CI file the PR changes. It is 🟡 when the same job loses its runner in every attempt on this PR but not on `main`: the PR may make it exhaust the runner (memory, disk).
- **No link found is not independence.** That is 🟡 or ⚪.
- **Fail-fast cascades and gate jobs** get no verdict of their own; mention them with their root failure.

Secondary, briefly: if upstream stages failed and later jobs were skipped, say PR CI is incomplete and name the workflow and stage; pending jobs are still running, not passing. Mention coverage only when evidence shows the PR's central change ran in no executed job.

### Output

Your first output line is the report heading — no preamble. Keep this skeleton and drop empty sections. AMD failures (workflow file names containing `amd`) come before others. Link jobs as `https://github.com/sgl-project/sglang/actions/runs/{run_id}/job/{job_id}` and log lines as `…/job/{job_id}#step:{step}:{line}`.

```
## CI Status for PR #N

**Verdict (1–2 sentences):** are any executed failures caused by this PR? Is PR CI complete?

> [!CAUTION] / [!WARNING] / [!NOTE]   (only for a related failure, incomplete CI, or an evidence-backed coverage gap)

**Executed failures:** AMD: <n> (<k> related) · Others: <n> (<k> related)

### AMD Executed Failures
| Job | Test | Failure | On `main` | Related? | Why |
|-----|------|---------|-----------|----------|-----|
| [job](url) | `file.py::case` | signature + mode, [log](line-url) | e.g. passed 0.95 ×2, crashed ×2, never this mode | 🔴 / 🟡 / 🟢 / ⚪ | the evidence that decided it, and what argues against it |

### Other Executed Failures
(same columns)

### What to do before merge
For each 🔴 / 🟡 / ⚪ row: the one most informative next step (e.g. an A/B with a specific flag, a rerun on a specific GPU) and what outcome would confirm or refute the verdict.
```

End the report with a machine-readable verdict block: one entry per failure ID in `evidence.md` that needs a verdict (cascades excluded). The harness checks it against the evidence pack and marks rows it cannot verify. Readers do not see it.

```
<!-- ci-status-verdicts
{"verdicts": [{"failure_id": "F1", "label": "related", "basis": "mechanism_chain",
  "mechanism": {"change": ["D3"], "execution": ["F1.markers"], "failure": ["F1.metrics"]},
  "evidence": ["F1.env", "F1.M5"], "counter_evidence": ["F1.M2"], "next_step": "…"}]}
-->
```

- `label` (`related`, `possibly_related`, `unrelated`, `unknown`) must match the table row.
- `basis`:
  - `related` → `mechanism_chain`
  - `possibly_related` → `suspicion`
  - `unrelated` → `comparable_recurrence` or `independence`
  - `unknown` → `insufficient_evidence`
- Cite evidence-pack IDs; evidence you found beyond the pack goes in the report text, with links.
- `mechanism` is required for `related` and needs at least one pack ID per link: a `D…` hunk for the change, this failure's `F….env` / `launch` / `markers` / `failure` for execution, and its `F….failure` / `metrics` for the failure. Leave it as empty lists otherwise.
- `comparable_recurrence` must cite the comparable `F….M…` records; `independence` must cite `D…` hunks and this failure's `F….failure`.
