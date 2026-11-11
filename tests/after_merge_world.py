"""The after-merge tests' world: real git against bare repositories standing in for GitHub.

The queue's promises are about refs -- what is read from where, what is pushed, what is
left behind -- so its tests drive real git, and `gh`, the session launch, the session's
end and the clock are the seams. A module rather than a `conftest.py`, for the reason
`support.py` gives.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import replace
from pathlib import Path

from support import load_script

after_merge_pass = load_script("scripts/after-merge-pass.py")
am = after_merge_pass.after_merge
jc = after_merge_pass.job_claims
triage = after_merge_pass.triage
fix_reports = after_merge_pass.fix_reports

PLENTY = 32 * 1024
DAY = 86_400.0
TOUCH = 'run = "echo ran>> ran.txt"\nwhen = "merged"\n'


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def out(cwd: Path, *args: str) -> str:
    return git(cwd, *args).stdout.strip()


class World:
    """One project on a remote, its static checkout, and what the seams were told."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.ws = base / "ws"
        self.ws.mkdir()
        self.launches: list[tuple[str, str]] = []
        self.trees: list[Path] = []
        self.launch_code = 0
        self.free_mb: int | None = PLENTY
        self.docker = False
        self.slept: list[float] = []
        self.merged_prs: set[tuple[str, int]] = set()
        self.merged_branches: set[str] = set()
        self.host = "pc-a"
        self.spool = base / "spool"
        self.mode = "dispatch"
        self.session = fix_reports.WORKING
        self.clock: float | None = None
        self.projects: list[str] = []
        self.add_project("carameli")

    @property
    def devkit(self) -> Path:
        """This machine's own `logs/`: two PCs share nothing but the remote."""
        root = self.base / f"devkit-root-{self.host}"
        root.mkdir(exist_ok=True)
        return root

    def add_project(self, name: str) -> None:
        remote = self.base / f"{name}.git"
        git(self.base, "init", "-q", "--bare", "-b", "main", str(remote))
        seed = self.base / f"{name}-seed"
        seed.mkdir()
        git(seed, "init", "-q", "-b", "main")
        git(seed, "remote", "add", "origin", remote.as_posix())
        self.write(name, "README.md", "hello\n")
        git(self.base, "clone", "-q", remote.as_posix(), str(self.ws / name))
        self.projects.append(name)
        self.write_workspace()

    def write_workspace(self) -> None:
        folders = [{"path": name} for name in self.projects]
        settings = {"devkit.fixPass": self.mode}
        self.workspace.write_text(
            json.dumps({"folders": folders, "settings": settings}), encoding="utf-8"
        )

    @property
    def workspace(self) -> Path:
        return self.ws / "w.code-workspace"

    def write(self, project: str, path: str, text: str) -> None:
        """Commit `path` to `main` on the remote, as a merged PR would."""
        seed = self.base / f"{project}-seed"
        target = seed / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "--no-verify", "-m", f"write {path}")
        git(seed, "push", "-q", "--no-verify", "origin", "main")

    def remove(self, project: str, path: str) -> None:
        seed = self.base / f"{project}-seed"
        (seed / path).unlink()
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "--no-verify", "-m", f"revert {path}")
        git(seed, "push", "-q", "--no-verify", "origin", "main")

    def identity(self, path: str, project: str = "carameli") -> str:
        """The id the queue gives `path`: its path and the commit that added it."""
        seed = self.base / f"{project}-seed"
        added = out(seed, "log", "--diff-filter=A", "-1", "--format=%H", "--", path)
        return am.task_id(path, added)

    def queue(self, text: str, name: str = "job", project: str = "carameli") -> str:
        path = f"{am.QUEUE_DIR}/{name}.toml"
        self.write(project, path, text)
        return self.identity(path, project)

    def launch(self, task, board, prompt):
        """A launch that cuts a stamped tree, as the real one does."""
        self.launches.append((task.name, prompt))
        if self.launch_code:
            return self.launch_code, "the launcher said no"
        tree = self.base / "trees" / f"{task.name}-{len(self.trees)}"
        (tree / "logs").mkdir(parents=True)
        key = f"{am.SESSION_KEY}{am.seen_key(task)}"
        fix_reports.stamp(tree, key, "after-merge", problem=key, agent="claude-bg")
        self.trees.append(tree)
        return 0, "", str(tree), f"agent/after-merge-{task.name}-{len(self.trees)}"

    def ship_from(self, tree: Path) -> None:
        """What a session finishing does: an intent in its tree."""
        (tree / "logs" / "ship-intent.md").write_text("Subject\n\nBody\n", encoding="utf-8")

    def gh_for(self, path: Path):
        def gh(*args):
            if args[:1] == ("api",):
                number = int(args[1].rsplit("/", 1)[1])
                repo = args[1].split("/")[2]
                answer = "true" if (repo, number) in self.merged_prs else "false"
                return subprocess.CompletedProcess(args, 0, f"{answer}\n", "")
            if args[:2] == ("pr", "list") and "--head" in args:
                merged = args[args.index("--head") + 1] in self.merged_branches
                return subprocess.CompletedProcess(
                    args, 0, '[{"number": 1}]' if merged else "[]", ""
                )
            return subprocess.CompletedProcess(args, 0, "[]\n", "")

        return gh

    def now(self) -> float:
        return time.time() if self.clock is None else self.clock

    def advance(self, days: float) -> None:
        self.clock = self.now() + days * DAY

    def seams(self, **changes):
        base = after_merge_pass.Seams(
            root=self.devkit,
            host=self.host,
            now=self.now,
            sleep=self.slept.append,
            gh_for=self.gh_for,
            free_mb=lambda: self.free_mb,
            docker=lambda: self.docker,
            launch=self.launch,
            session_of=lambda tree, now: self.session,
            provision=lambda tree: [],
            spool=self.spool,
        )
        return replace(base, **changes)

    def run(self, **changes) -> int:
        self.write_workspace()
        seams = self.seams(**changes)
        code = after_merge_pass.run_pass(self.workspace, [], seams)
        assert not [
            item for item in triage.load(seams.root) if "pass-step-crashed" in str(item.fields)
        ], "a step of the pass raised"
        return code

    def remote(self, project: str = "carameli") -> Path:
        return self.base / f"{project}.git"

    def on_remote(self, ref: str, project: str = "carameli") -> bool:
        return bool(out(self.remote(project), "rev-parse", "--verify", "--quiet", ref))

    def done(self, task_id: str, host: str = "", project: str = "carameli") -> bool:
        return self.on_remote(jc.done_ref(task_id, host), project)

    def claimed(self, task_id: str) -> bool:
        return self.on_remote(jc.claim_ref(task_id))

    def result(self, task_id: str, name: str = "job") -> dict:
        path = self.devkit / after_merge_pass.OUTPUT_DIR / f"carameli-{name}-{task_id}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def findings(self) -> list[str]:
        return [str(item.fields.get("detail", "")) for item in triage.load(self.devkit)]

    def ran(self) -> int:
        """How many times `TOUCH` has run, on any host: they share this checkout."""
        marks = self.checkout / "ran.txt"
        return len(marks.read_text(encoding="utf-8").splitlines()) if marks.exists() else 0

    @property
    def checkout(self) -> Path:
        return self.ws / "carameli"


def refusing_push(world: World):
    """A `git_for` whose every push fails as a network failure does."""
    real = world.seams().git_for

    def git_for(path):
        call = real(path)

        def fake(*args, env=None):
            if args[0] == "push":
                return subprocess.CompletedProcess(args, 128, "", "fatal: unable to access")
            return call(*args, env=env)

        return fake

    return git_for


def make_world(tmp_path: Path, monkeypatch) -> World:
    """A world whose git sees neither this machine's global hooks nor its identity."""
    config = tmp_path / "gitconfig"
    config.write_text("[user]\n\tname = t\n\temail = t@example.com\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return World(tmp_path)
