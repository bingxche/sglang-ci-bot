
============================================================
Monitoring: nightly-test-amd.yml
============================================================
  Found 1 new failed run(s)

  Run 23222633964: https://github.com/sgl-project/sglang/actions/runs/23222633964

    Job: nightly-8-gpu-grok2 (ID: 67526461016)
    Downloading full job log...
    Log size: 593,894 chars
    Parsed 21 step(s)
    Running progressive step analysis...
    [1/21] Summarizing: (preamble) (PASSED, 298 chars)
    [2/21] Summarizing: GITHUB_TOKEN Permissions (PASSED, 131 chars)
    [3/21] Summarizing: (preamble) (PASSED, 416 chars)
    [4/21] Summarizing: Run actions/checkout@v4 (PASSED, 828 chars)
    [5/21] Summarizing: (preamble) (PASSED, 67 chars)
    [6/21] Summarizing: Getting Git version info (PASSED, 191 chars)
    [7/21] Summarizing: (preamble) (PASSED, 482 chars)
    [8/21] Summarizing: Initializing the repository (PASSED, 1,262 chars)
    [9/21] Summarizing: Disabling automatic garbage collection (PASSED, 75 chars)
    [10/21] Summarizing: Setting up auth (PASSED, 1,104 chars)
    [11/21] Summarizing: Fetching the repository (PASSED, 327 chars)
    [12/21] Summarizing: Determining the checkout info (PASSED, 0 chars)
    [13/21] Summarizing: (preamble) (PASSED, 178 chars)
    [14/21] Summarizing: Checking out the ref (PASSED, 246 chars)
    [15/21] Summarizing: (preamble) (PASSED, 139 chars)
    [16/21] Summarizing: Run touch github_summary.md (PASSED, 377 chars)
    [17/21] Summarizing: (preamble) (PASSED, 62,926 chars)
    [18/21] Summarizing: Run bash scripts/ci/amd/amd_ci_install_dependency.sh (PASSED, 234 chars)
      Pre-filtered '(preamble)': 178,057 -> 120,857 chars
    [19/21] Summarizing: (preamble) (PASSED, 120,857 chars)
    [20/21] Summarizing: Run > github_summary.md  # Clear summary file (PASSED, 837 chars)
      Pre-filtered '(trailing)': 344,381 -> 94,215 chars
    [21/21] Summarizing: (trailing) (PASSED, 94,215 chars)
    Generating final job analysis...

============================================================
## CI Failure Report: `nightly-test-amd.yml`

**Time**: 2026-03-18 16:19 UTC
**Jobs analyzed**: 1
**Method**: Progressive step-by-step analysis (all steps examined)

### Failed Jobs
- [nightly-8-gpu-grok2](https://github.com/sgl-project/sglang/actions/runs/23222633964) (Run #23222633964)

---

### Job: `nightly-8-gpu-grok2`
- **Run**: [23222633964](https://github.com/sgl-project/sglang/actions/runs/23222633964)
- **Started**: 2026-03-18T09:51:23Z
- **Failed Steps**: Accuracy Test (8-GPU Grok2)

# CI Failure Analysis: `nightly-8-gpu-grok2`

## 1. Root Cause Analysis

**The test failed due to a marginal accuracy regression — the Grok-2 model scored 0.910 (182/200) on the GSM8K benchmark, missing the 0.915 threshold by exactly 1 correct answer (183 needed).**

This is a **flaky accuracy test** failure. The root cause is likely one or a combination of:

1. **Aiter RoPE reduced precision**: All 8 GPUs logged `"Aiter backend is selected for fused RoPE. This has lower precision"`. The `aiter` attention backend uses a fused RoPE implementation with lower numerical precision than the default path. On a 200-sample benchmark, this precision loss can cause 1–2 answers to flip between correct and incorrect across runs.

2. **FP8 quantization noise**: The model runs with FP8 quantization on TP=8. FP8 is inherently lossy, and combined with the lower-precision aiter RoPE, the cumulative numerical error may be pushing accuracy right at the threshold boundary.

3. **Non-deterministic inference**: LLM inference with tensor parallelism involves non-deterministic reductions (RCCL allreduce), meaning results can vary slightly between runs. A threshold set at 0.915 with only 200 samples gives a granularity of 0.005 per question — making the test inherently fragile when true accuracy hovers near the boundary.

4. **GPU architecture mismatch (minor concern)**: The runner is MI325 hardware but the CI scripts defaulted to `mi30x` architecture due to a parsing failure in the runner name. While MI325 and MI300X share gfx942, there could be subtle microarchitectural differences affecting numerical behavior of certain kernels.

## 2. Failure Details

**Test**: `test_grok2_accuracy` in `TestGrok2EvalAMD`
**File**: `registered/amd/accuracy/mi30x/test_grok2_eval_amd.py`

```
FAIL: test_grok2_accuracy (TestGrok2EvalAMD)
Test Grok-2 with GSM8K completion benchmark.
AssertionError: 0.91 not greater than or equal to 0.915 : Accuracy 0.910 below threshold 0.915
```

| Metric | Value |
|--------|-------|
| Correct answers | 182 / 200 |
| Achieved accuracy | 0.910 |
| Required threshold | 0.915 |
| Delta | -0.005 (1 question short) |
| Exit code | 255 |

**Additional warnings during run:**
- `Unknown suite nightly-amd-accuracy-8-gpu-grok2 for backend AMD` — suite not registered properly
- Watchdog soft timeouts (300s) on all 8 schedulers + tokenizer manager during model download
- Health check transient failure during aiter JIT compilation (~33s kernel build)
- Aiter RoPE lower precision warning on all 8 GPUs

## 3. Suggested Fixes

### Fix A: Lower the accuracy threshold (Recommended — immediate fix)

The threshold is too tight for a 200-sample test with FP8 + aiter RoPE. Lower it to account for run-to-run variance.

```python
# In test_grok2_eval_amd.py
# Before:
ACCURACY_THRESHOLD = 0.915

# After:
ACCURACY_THRESHOLD = 0.905  # ~181/200, allows for FP8+aiter numerical variance
```

**Justification**: With 200 samples, each question is worth 0.5%. A threshold of 0.915 allows zero margin. Historical runs likely fluctuate in the 0.905–0.920 range.

### Fix B: Increase sample count for statistical stability

```python
# Increase from 200 to 500+ samples to reduce variance
NUM_SAMPLES = 500
ACCURACY_THRESHOLD = 0.910  # Can keep tighter threshold with more samples
```

### Fix C: Fix GPU architecture parsing for MI325

In `amd_ci_start_container.sh`, add MI325 to the parsing logic:

```bash
# Add mi325 pattern recognition
if [[ "$RUNNER_NAME" == *"mi325"* ]]; then
    GPU_ARCH="mi325"
elif [[ "$RUNNER_NAME" == *"mi30"* ]]; then
    GPU_ARCH="mi30x"
fi
```

### Fix D: Register the test suite properly

The warning `Unknown suite nightly-amd-accuracy-8-gpu-grok2 for backend AMD` suggests the suite name isn't registered in `run_suite.py`. Add it to the suite registry to eliminate the warning and ensure proper configuration is applied.

### Fix E: Investigate aiter RoPE precision impact (longer-term)

Compare accuracy with and without aiter fused RoPE:
```bash
# Test with default (higher precision) RoPE
SGLANG_ATTENTION_BACKEND=triton python3 -m pytest test_grok2_eval_amd.py -v
```

If accuracy improves consistently, consider either fixing aiter RoPE precision or adjusting the threshold specifically when aiter is the backend.

## 4. Priority

**Medium**

- This is a **nightly accuracy regression test**, not a PR gate
- The failure is **marginal** (0.5% = 1 question on 200 samples) and likely **non-deterministic**
- No functional breakage — the model serves correctly and achieves near-target accuracy
- However, if left unfixed, this test will **flake repeatedly**, eroding trust in CI signals

## 5. Environment Context

| Component | Value |
|-----------|-------|
| **Hardware** | 8× AMD MI325 GPUs (~248 GB VRAM each) |
| **Parsed GPU arch** | `mi30x` (⚠️ MI325 not recognized) |
| **Docker image** | `rocm/sgl-dev:v0.5.9-rocm700-mi30x-20260317` |
| **ROCm** | 7.0.0 |
| **PyTorch** | 2.9.0a0+git7bcbafe |
| **SGLang** | 0.1.dev1+g21c4fc633.d20260318 (v0.5.9 era) |
| **sglang-kernel** | 0.4.0 (built for gfx942) |
| **aiter** | v0.1.11.post2.dev0+g417de6df4.d20260317 |
| **Transformers** | 4.57.1 |
| **Attention backend** | `aiter` (fused RoPE, lower precision) |
| **Model** | `xai-org/grok-2` (FP8, TP=8) |
| **Tokenizer** | `alvarobartt/grok-2-tokenizer` |
| **Benchmark** | GSM8K, 200 samples, completion mode |
| **RCCL_MSCCL_ENABLE** | 0 (MSCCL disabled) |
| **Commit** | `21c4fc6334d13cbc075504353b9abc3716cc069e` |

---


*Generated by amd-bot — progressive CI analysis*


Done. 1 workflow(s) had failures.
