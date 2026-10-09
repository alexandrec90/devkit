"""CI container images must not come from Docker Hub anonymously.

A GitHub-hosted runner pulls from Docker Hub unauthenticated, on a shared IP, so the pull
budget is spent by strangers: social-scraper's PR gate went red on `main` (2026-10-09)
with `toomanyrequests` pulling `postgres:18` for its service container, before a single
test ran -- and that block was rendered from devkit's `pr-gate.yml.tmpl` (536d3de4), so
every generated project with a database carried the same failure. Naming a registry host
-- ECR Public's mirror of the Docker Official Images, `public.ecr.aws/docker/library/...`
-- takes the pull off that budget.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# The workflows devkit runs and the ones it renders into every generated project.
WORKFLOWS = [
    *sorted((ROOT / ".github" / "workflows").glob("*.y*ml")),
    *sorted((ROOT / "templates").glob("**/dot-github/workflows/*.y*ml*")),
]
HUB = (None, "docker.io", "registry-1.docker.io", "index.docker.io")

_IMAGE = re.compile(r"^\s*(?:-\s*)?image:\s*['\"]?([^'\"\s#]+)", re.MULTILINE)


def registry_host(image: str) -> str | None:
    """The registry an image reference names, or None when it means Docker Hub.

    Docker's own rule: the first `/`-separated component is a registry only if it
    contains a `.` or a `:`, or is `localhost`. `postgres:18` and `library/postgres` are
    both Docker Hub.
    """
    first, sep, _ = image.partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        return first
    return None


def workflow_images() -> list[tuple[str, str]]:
    return [
        (path.relative_to(ROOT).as_posix(), match.group(1))
        for path in WORKFLOWS
        for match in _IMAGE.finditer(path.read_text(encoding="utf-8"))
    ]


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("postgres:18", None),
        ("library/postgres:18", None),
        ("docker.io/library/postgres:18", "docker.io"),
        ("public.ecr.aws/docker/library/postgres:18", "public.ecr.aws"),
        ("localhost/postgres", "localhost"),
        ("registry:5000/postgres", "registry:5000"),
    ],
)
def test_registry_host(image: str, expected: str | None) -> None:
    assert registry_host(image) == expected


def test_the_rendered_workflows_name_images():
    """Guards the test below against passing vacuously if the pattern stops matching."""
    names = {name for name, _ in workflow_images()}
    assert "templates/core/dot-github/workflows/pr-gate.yml.tmpl" in names
    assert "templates/core/dot-github/workflows/nightly.yml.tmpl" in names


def test_no_workflow_pulls_from_docker_hub():
    hub = [f"{name}: {image}" for name, image in workflow_images() if registry_host(image) in HUB]
    assert not hub, f"pulled anonymously from Docker Hub in CI: {hub}"
