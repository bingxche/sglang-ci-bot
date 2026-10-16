import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import utils  # noqa: E402


DOCKERFILE = """\
ARG GPU_ARCH=gfx950
FROM base:942 AS gfx942
ENV BUILD_TRITON="0"
ENV HSA_ENABLE_IPC_MODE_LEGACY=1

FROM ${GPU_ARCH}
RUN python3 - <<'PY'
from pathlib import Path
ENV_NOT_REAL = 1
PY
ENV SGLANG_USE_AITER=1
ENV A=1 \\
    B="two words"
ENV LEGACY_KEY legacy value
RUN echo hi <<<"x"
ENV ROCM_QUICK_REDUCE_QUANTIZATION=INT8
"""

EXEC_SH = """\
declare -A ENV_MAP=(
  [SGLANG_IS_IN_CI]=1
  # [COMMENTED_OUT]=1
  [SGLANG_USE_AITER]=1
)
if [[ "${GPU_FAMILY}" == "mi35x" ]]; then
  ENV_MAP[GPU_ARCHS]="gfx950"
fi
"""

START_SH = """\
docker run -dt --user root \\
  -e HF_HOME=/sgl-data/hf-cache \\
  -e HF_TOKEN="${HF_TOKEN:-}" \\
  rocm/sgl-dev:latest
"""

WORKFLOW = """\
jobs:
  stage-c:
    steps:
      - run: |
          bash scripts/ci/amd/amd_ci_exec.sh -e SGLANG_USE_ROCM700A=1 python3 run_suite.py
"""


class TestParseDockerfileEnv(unittest.TestCase):
    def test_stages_continuations_heredocs_and_legacy_form(self):
        entries = utils._parse_dockerfile_env(DOCKERFILE)
        by_key = {key: (stage, line, value) for stage, line, key, value in entries}

        self.assertEqual(by_key["BUILD_TRITON"], ("gfx942", 3, "0"))
        self.assertEqual(by_key["HSA_ENABLE_IPC_MODE_LEGACY"][0], "gfx942")
        self.assertEqual(by_key["SGLANG_USE_AITER"][0], "FROM ${GPU_ARCH}")
        self.assertEqual(by_key["A"][2], "1")
        self.assertEqual(by_key["B"][2], "two words")
        self.assertEqual(by_key["LEGACY_KEY"][2], "legacy value")
        # A here-string must not be mistaken for a heredoc that swallows the
        # rest of the file.
        self.assertEqual(by_key["ROCM_QUICK_REDUCE_QUANTIZATION"][2], "INT8")
        # Heredoc bodies are not Dockerfile instructions.
        self.assertNotIn("ENV_NOT_REAL", by_key)
        self.assertNotIn("pathlib", " ".join(stage for stage, *_ in entries))


class TestParseShellEnv(unittest.TestCase):
    def test_env_map_and_docker_flags(self):
        exec_keys = {k: v for _, k, v in utils._parse_shell_env(EXEC_SH)}
        self.assertEqual(
            exec_keys,
            {"SGLANG_IS_IN_CI": "1", "SGLANG_USE_AITER": "1", "GPU_ARCHS": "gfx950"},
        )

        start_keys = {k: v for _, k, v in utils._parse_shell_env(START_SH)}
        self.assertEqual(
            start_keys,
            {"HF_HOME": "/sgl-data/hf-cache", "HF_TOKEN": "${HF_TOKEN:-}"},
        )


class TestCollectCiRuntimeEnv(unittest.TestCase):
    def test_renders_all_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            files = {
                "docker/rocm.Dockerfile": DOCKERFILE,
                "scripts/ci/amd/amd_ci_exec.sh": EXEC_SH,
                ".github/workflows/pr-test-amd.yml": WORKFLOW,
            }
            for rel, content in files.items():
                path = repo / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)

            report = utils.collect_ci_runtime_env(repo)

        self.assertIn("`ROCM_QUICK_REDUCE_QUANTIZATION=INT8` (stage `FROM ${GPU_ARCH}`, L16)", report)
        self.assertIn("## `scripts/ci/amd/amd_ci_exec.sh`", report)
        self.assertIn("`SGLANG_USE_AITER=1` (L4)", report)
        self.assertIn("## `.github/workflows/pr-test-amd.yml`", report)
        self.assertIn("`SGLANG_USE_ROCM700A=1` (L5)", report)
        # Missing sources are reported, not silently dropped.
        self.assertIn(
            "## `scripts/ci/amd/amd_ci_start_container.sh`\n\n*(file not found in this checkout)*",
            report,
        )


if __name__ == "__main__":
    unittest.main()
