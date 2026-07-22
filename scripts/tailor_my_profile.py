"""Manual, one-off script — NOT part of the pytest suite.

Runs the real tailoring pipeline end to end: your actual profile.json,
a real job description, a REAL LLM call (agent.llm_client.TaskLLMClient,
using MODEL / MODEL_API_KEY from .env), and the real tectonic renderer.
Meant to be run by hand to eyeball a tailored CV, not on every test run —
it costs a real API call.

Usage:
    PYTHONPATH=src python scripts/tailor_my_profile.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agent.llm_client import TaskLLMClient  # noqa: E402
from profile.schema import Profile  # noqa: E402
from tailoring.tailor import TailoringError, tailor  # noqa: E402

PROFILE_PATH = REPO_ROOT / "profile.json"
TEMPLATE_PATH = REPO_ROOT / "src" / "tailoring" / "templates" / "cv.tex.jinja"
OUTPUT_DIR = REPO_ROOT / "tailoring_output" / "robotics_swe"

JOB_DESCRIPTION = """\
Title: Robotics Software Engineer
Job ID: 20896
Location: Land - 249 Jalan Boon Lay, SG

Job Descriptions:

- Perform design, implementation, and deployment of advanced software modules for robotics systems, such as perception, localisation, navigation, machine learning, or robotics management
- Develop, optimise and test software algorithm APIs under Windows and/or embedded Linux environments
- Develop validation and verification test plans, to ensure that the engineering deliverables meet both customer goals and internal specifications as well as troubleshooting
- Participate in meetings with cross-functional teams to solicit inputs for continual improvement process
- Conduct trials to collect data and evaluate the attribute or capability of the software modules. Perform quality assurance to ensure it meets the expected results
- Support the testing/deployment engineer in defining DOE (design of experiment) procedures, analysing and documenting the result
- Support the software lead in administration or software documentation when required
- Troubleshooting robotics systems in both simulation and physical system
- Static code analysis, unit testing and code coverage
- Perform system deployment, integration, tests and project documentation
- Communicate with internal/external customers on project requirements/progress and on-site system implementation

Requirements:

- At least a Degree in Computer Science, Electrical/Mechatronics/Mechanical Engineering (related discipline or equivalent)
- Entry level candidates are welcome to apply.
- Knowledge or experience related to C, C++
- Knowledge or experience related to Python programming is an added advantage
- Knowledge of ROS is an added advantage
- Experience in embedded systems implementation, such as ARM, DSP or FPGA, would be an added advantage
- Having experience in technology development for robotics systems will be a plus, not mandatory
- Ability to contribute as a team player or independently
- Strong interpersonal and communication skills
- Ability to demonstrate a high level of initiative and resourcefulness
"""


class LoggingLLM:
    """Wraps the real TaskLLMClient just to keep the raw response around —
    if tailor() raises, we still want to see exactly what the LLM said."""

    def __init__(self, client: TaskLLMClient):
        self._client = client
        self.last_prompt: str | None = None
        self.last_response: str | None = None

    async def complete(self, prompt: str) -> str:
        self.last_prompt = prompt
        self.last_response = await self._client.complete(prompt)
        return self.last_response


async def main() -> None:
    profile = Profile.model_validate(json.loads(PROFILE_PATH.read_text()))
    llm = LoggingLLM(TaskLLMClient())

    print(f"Tailoring for: {profile.name}")
    print(f"Job: Robotics Software Engineer (Job ID 20896)")
    print("Calling the LLM...")

    try:
        result = await tailor(
            JOB_DESCRIPTION,
            profile,
            llm=llm,
            template_path=TEMPLATE_PATH,
            output_dir=OUTPUT_DIR,
            save_debug_artifacts=True,
        )
    except TailoringError as e:
        print(f"\nTailoring FAILED — reason: {e.reason}")
        if e.violations:
            print("Violations:")
            for v in e.violations:
                print(f"  - {v}")
        if llm.last_response is not None:
            raw_path = OUTPUT_DIR / "debug" / "raw_llm_response.json"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(llm.last_response)
            print(f"Raw LLM response saved to: {raw_path}")
        raise SystemExit(1)

    print(f"\nSuccess. CV written to: {result.path}")
    print(f"Debug .tex + raw prompt at: {OUTPUT_DIR / 'debug'}")


if __name__ == "__main__":
    asyncio.run(main())
