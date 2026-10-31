"""Run the deterministic parts of the bundled YouTube creator skills."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from pipeline.run_store import write_json


SKILLS_DIR = Path(__file__).resolve().parent.parent / ".github" / "skills"


def _run(script: Path, *args: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(script), *args, "--json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def prepare_skill_workflow(directory: Path, fact: dict, captions: Path) -> list[Path]:
    """Create an auditable skill report before the publishing stage."""
    title = _run(
        SKILLS_DIR / "yt-package" / "title.py",
        "--title",
        fact["title"],
    )[0]
    hook = _run(
        SKILLS_DIR / "yt-script" / "hookscore.py",
        "--hook",
        fact["fact_text"].split(".")[0].strip(),
    )
    report = {
        "workflow": "bundled-youtube-skills",
        "skills": ["yt-script", "yt-package", "yt-seo"],
        "title_package": title,
        "hook_score": hook,
        "seo_metadata": "pipeline youtube_metadata.json",
        "captions": str(captions.name),
        "note": "Prompt-only skills guide the generated script; deterministic skill tools are recorded here.",
    }
    path = directory / "skill_workflow.json"
    write_json(path, report)
    return [path]
