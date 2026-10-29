## Suspect PR Verification

Today's Daily Cross-Workflow Summary blames a merged sglang PR for a cluster of AMD CI failures, and amd-bot is about to comment on that PR to tell its author. You are one of several independent verifiers; the comment is posted only if most of you confirm. A wrong comment points an upstream author at a break that is not theirs, so look hard for reasons the attribution is wrong.

The prompt gives the cluster, the suspect PR and its commit, the last passing and first failing sglang commits, and the failing jobs. `.ci-context/suspect.md` has the summary's evidence for this suspect and the per-job analyses of the cluster's jobs. The current directory is sglang at the first failing commit.

Check:

1. **The change.** Read the PR's diff (`git show <commit>`, or the GitHub API for the PR's files). Does it change code on the path the failing tests run or that appears in the stack traces? Name the file and function that connect the change to the failure.
2. **The failures.** Download and read the logs of at least two failing jobs yourself (GitHub API, `/actions/jobs/<id>/logs`). Is the failure signature what that change would cause? `suspect.md` is the case you are checking, not evidence for it: never confirm from its analyses alone. If you could not read any failing log, vote `uncertain`.
3. **Other explanations.** List the commits in `last_pass..first_fail` (`git log --oneline`). Another commit touching the same path, a change of CI image or dependency (aiter, ROCm, torch), or a runner or infrastructure failure is an alternative. An alternative you cannot rule out is a reason not to confirm.
4. **Later runs.** If a failing test passed on a commit after the suspect PR, without a fix in between, the attribution is wrong.

Vote:

- `confirmed`: the chain holds (the change, then the changed path running in these jobs, then this failure), and no alternative is left that you could not exclude.
- `refuted`: the evidence contradicts the attribution.
- `uncertain`: neither.

`logs_read` lists the job IDs whose logs you read. Your final message is only this block:

```
<!-- suspect-vote
{"verdict": "confirmed", "logs_read": [122989040460, 122989040546],
 "reason": "Two to four sentences citing file:function, commits and job links."}
-->
```
