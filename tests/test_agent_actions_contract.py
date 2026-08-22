"""Run the TS contract tests via node (tsx-less: strip types with a regex-free approach).

The repo has no vitest/jest; existing TS tests are plain scripts. We execute
them through node --experimental-strip-types when available, else tsc-free skip.
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


def run_node(script: Path) -> tuple[int, str]:
    node = shutil.which("node")
    if not node:
        return 1, "node not found"
    result = subprocess.run(
        [node, "--experimental-strip-types", str(script)],
        capture_output=True,
        text=True,
        cwd=str(script.parent.parent),
        timeout=60,
    )
    return result.returncode, (result.stdout + result.stderr).strip()


class AgentActionsContractTest(unittest.TestCase):
    def test_agent_actions_contract(self) -> None:
        script = Path(__file__).resolve().parents[1] / "tests" / "agent-actions-contract.test.ts"
        code, output = run_node(script)
        self.assertEqual(code, 0, f"contract test failed:\n{output}")
        self.assertIn("agent action contract: OK", output)


if __name__ == "__main__":
    unittest.main()
