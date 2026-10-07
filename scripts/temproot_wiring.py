#!/usr/bin/env python3
"""Wire the vendored temp-root plugin into a consumer's own pytest configuration.

`scripts/pytest-plugins/devkit_temproot.py` gives every pytest run a temp root of its own,
so a run elsewhere on the machine cannot fail this one with `WinError 5` at its exit. It
is vendored, so `sync-devkit.py --pull` delivers it -- and loads it nowhere: what loads it
is `-p devkit_temproot` in `addopts` and its directory in `pythonpath`, and both live in a
file the project owns. #424 vendored the plugin, every consumer adopted it, and none of
them loaded it (7f011bf8). The generated template is wired; a project generated before it
was is wired by nothing but this.

So `upgrade-project.py` runs `wire` in every adoption box, after the pull and before the
commit, and the adoption PR carries the wiring through that project's own gate. It is a
text edit rather than a rewrite -- a project's config holds its comments and its order --
and it refuses any shape it cannot edit with certainty, saying why, rather than guess:
an unwired project is the state before, not a broken one.

Three edits, all needed together:

- **the `pytest>=` floor to 9.1**, the first pytest that applies `pythonpath` before it
  loads an `addopts` plugin; an older one fails at startup with `ModuleNotFoundError`;
- **`-p devkit_temproot` and the plugin directory**, in `pytest.ini` when the project has
  one (pytest reads it ahead of `pyproject.toml`), else in `[tool.pytest.ini_options]`;
- **a relock** where `uv.lock` records the floor, or `uv sync --locked` refuses the tree.

The same box runs `unquiet` on the same file: it drops a `-q` from `addopts`. The
template lost its own long ago (an agent adds one, and `-qq` prints no "N passed" line),
but a template is a one-shot copy, so every project rendered before kept it, and a session
there re-ran unchanged tests to see a result the output never held (8bbdd581).

`python scripts/temproot_wiring.py <project>` does the same by hand. Tested in
`tests/test_temproot_wiring.py`.
"""

from __future__ import annotations

import configparser
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

import sweep

PLUGIN = "devkit_temproot"
PLUGIN_DIR = "scripts/pytest-plugins"
PLUGIN_FILE = Path(PLUGIN_DIR) / f"{PLUGIN}.py"
LOAD = f"-p {PLUGIN}"
FLOOR = (9, 1)
FLOOR_TEXT = ".".join(str(part) for part in FLOOR)

WIRED = "wired"
ALREADY = "already wired"
UNQUIETED = "dropped `-q` from addopts"
NOT_QUIET = "no `-q` in addopts"

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

# `pytest>=X` as a requirement, not `pytest-xdist>=` or `my-pytest>=`.
_FLOOR = re.compile(r"(?<![\w.-])pytest>=([\d.]+)(?![\d.])")
_PIN = re.compile(r"^pytest==([\d.]+)\s*(?:#.*)?$", re.M)
_SECTION = re.compile(r"^\[tool\.pytest\.ini_options\][ \t]*$", re.M)
_INI_SECTION = re.compile(r"^\[pytest\][ \t]*$", re.M)
_NEXT_SECTION = re.compile(r"^\[", re.M)
# `-q`, `-qq` or `--quiet` as a word of an `addopts` value, with the space before it.
_QUIET = re.compile(r"\s*(?<!\S)(?:-q+|--quiet)(?!\S)")
# What precedes an ini `addopts` value on its line: the key, or a continuation's indent.
_INI_HEAD = re.compile(r"addopts[ \t]*=[ \t]*|[ \t]*")


def version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split(".") if part)


def raise_floor(text: str) -> tuple[str, bool]:
    """`text` with every `pytest>=` floor below 9.1 raised to it; `(text, found one)`."""
    found = bool(_FLOOR.search(text))

    def lift(match: re.Match[str]) -> str:
        return match.group(0) if version(match.group(1)) >= FLOOR else f"pytest>={FLOOR_TEXT}"

    return _FLOOR.sub(lift, text), found


def _span(text: str, header: re.Pattern[str]) -> tuple[int, int] | None:
    """Where the section under `header` starts (after its header line) and ends."""
    match = header.search(text)
    if match is None:
        return None
    start = text.index("\n", match.end()) + 1 if "\n" in text[match.end() :] else len(text)
    following = _NEXT_SECTION.search(text, start)
    return start, following.start() if following else len(text)


def _key_line(body: str, key: str) -> re.Match[str] | None:
    return re.search(rf"^{key}[ \t]*=.*$", body, re.M)


def wire_pyproject(text: str) -> tuple[str, str]:
    """`[tool.pytest.ini_options]` loading the plugin: `(new text, "")`, or `(text, why
    not)`. The section is added when there is none."""
    if re.search(r"^\[tool\.pytest\][ \t]*$", text, re.M):
        return text, "it configures pytest in a native [tool.pytest] table; left to a person"
    span = _span(text, _SECTION)
    if span is None:
        tail = "" if text.endswith("\n") else "\n"
        block = f'\n[tool.pytest.ini_options]\naddopts = "{LOAD}"\npythonpath = ["{PLUGIN_DIR}"]\n'
        return _checked_pyproject(text + tail + block)
    start, end = span
    body = text[start:end]
    addopts = _key_line(body, "addopts")
    if addopts is None:
        body = f'addopts = "{LOAD}"\n' + body
    else:
        single = re.fullmatch(r"addopts[ \t]*=[ \t]*([\"'])(.*)\1[ \t]*", addopts.group(0))
        if single is None:
            return text, "`addopts` is not a one-line string, so it is left to a person"
        quote, value = single.group(1), single.group(2).strip()
        line = f"addopts = {quote}{(value + ' ' + LOAD).strip()}{quote}"
        body = body[: addopts.start()] + line + body[addopts.end() :]
    body, why = _with_path(body)
    if why:
        return text, why
    return _checked_pyproject(text[:start] + body + text[end:])


def _with_path(body: str) -> tuple[str, str]:
    """The section body with the plugin directory on `pythonpath`; `(body, why not)`."""
    found = _key_line(body, "pythonpath")
    if found is None:
        addopts = _key_line(body, "addopts")
        at = addopts.end() + 1 if addopts else 0
        return body[:at] + f'pythonpath = ["{PLUGIN_DIR}"]\n' + body[at:], ""
    line = found.group(0)
    listed = re.fullmatch(r"pythonpath[ \t]*=[ \t]*\[(.*)\][ \t]*", line)
    single = re.fullmatch(r"pythonpath[ \t]*=[ \t]*([\"'])(.*)\1[ \t]*", line)
    if listed is not None:
        items = listed.group(1).strip().rstrip(",").strip()
        new = f'pythonpath = [{items + ", " if items else ""}"{PLUGIN_DIR}"]'
    elif single is not None:
        new = f'pythonpath = [{single.group(1)}{single.group(2)}{single.group(1)}, "{PLUGIN_DIR}"]'
    else:
        return body, "`pythonpath` is not a one-line list or string, so it is left to a person"
    return body[: found.start()] + new + body[found.end() :], ""


def _checked_pyproject(text: str) -> tuple[str, str]:
    """`(text, "")` when it parses and its table loads the plugin; else why not."""
    try:
        options = tomllib.loads(text).get("tool", {}).get("pytest", {}).get("ini_options", {})
    except tomllib.TOMLDecodeError as error:
        return text, f"the edited pyproject.toml does not parse: {error}"
    if not loads_plugin(options.get("addopts", ""), options.get("pythonpath", [])):
        return text, "the edited pyproject.toml does not load the plugin"
    return text, ""


def loads_plugin(addopts: object, pythonpath: object) -> bool:
    """Whether `-p devkit_temproot` is in `addopts` and its directory on `pythonpath`,
    each as a TOML array or as an ini's whitespace-separated string."""
    words = _words(addopts)
    loaded = any(
        (word == "-p" and after == PLUGIN) or word == f"-p{PLUGIN}"
        for word, after in zip(words, [*words[1:], ""], strict=True)
    )
    return loaded and PLUGIN_DIR in [path.rstrip("/") for path in _words(pythonpath)]


def _words(value: object) -> list[str]:
    if isinstance(value, str):
        return value.split()
    return [str(item) for item in value] if isinstance(value, list | tuple) else []


def wire_pytest_ini(text: str) -> tuple[str, str]:
    """`pytest.ini`'s `[pytest]` loading the plugin: `(new text, "")` or `(text, why not)`.

    A value may run on over indented continuation lines, so each addition goes at the end
    of the value's last line."""
    span = _span(text, _INI_SECTION)
    if span is None:
        return text, "pytest.ini has no [pytest] section"
    start, end = span
    lines = text[start:end].splitlines(keepends=True)
    lines, added = _append_ini(lines, "addopts", LOAD)
    if not added:
        lines.insert(0, f"addopts = {LOAD}\n")
    lines, added = _append_ini(lines, "pythonpath", PLUGIN_DIR)
    if not added:
        at = _value_end(lines, "addopts")
        lines.insert(at, f"pythonpath = {PLUGIN_DIR}\n")
    new = text[:start] + "".join(lines) + text[end:]
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(new)
        section = parser["pytest"]
    except (configparser.Error, KeyError) as error:
        return text, f"the edited pytest.ini does not parse: {error}"
    if not loads_plugin(section.get("addopts", ""), section.get("pythonpath", "")):
        return text, "the edited pytest.ini does not load the plugin"
    return new, ""


def _value_end(lines: list[str], key: str) -> int:
    """The index just past `key`'s value, continuation lines included; 0 when absent."""
    for index, line in enumerate(lines):
        if re.match(rf"{key}[ \t]*=", line):
            end = index + 1
            while end < len(lines) and lines[end][:1] in (" ", "\t") and lines[end].strip():
                end += 1
            return end
    return 0


def _append_ini(lines: list[str], key: str, word: str) -> tuple[list[str], bool]:
    """`lines` with `word` after `key`'s value; `(lines, whether the key was there)`."""
    end = _value_end(lines, key)
    if not end:
        return lines, False
    last = lines[end - 1]
    newline = "\n" if last.endswith("\n") else ""
    lines = [*lines]
    lines[end - 1] = f"{last.rstrip()} {word}{newline}"
    return lines, True


def _read(path: Path) -> str:
    """`path` with `\\n` line endings, whatever the checkout wrote."""
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _write(path: Path, text: str, like: bytes) -> None:
    """`text` to `path` in the line endings of `like`, the file as it was: a CRLF
    checkout keeps CRLF, so the diff is the edit and not every line."""
    ending = b"\r\n" if b"\r\n" in like else b"\n"
    path.write_bytes(text.encode("utf-8").replace(b"\n", ending))


def _pins_below_floor(root: Path) -> list[str]:
    """Compiled requirements pinning a pytest older than the floor: a recompile, not an edit."""
    return [
        f"{path.name} pins pytest=={match.group(1)}"
        for path in sorted(root.glob("requirements*.txt"))
        for match in _PIN.finditer(_read(path))
        if version(match.group(1)) < FLOOR
    ]


def plan(root: Path) -> tuple[dict[Path, str], str]:
    """`({file: new text}, "")` wiring `root`, or `({}, why not)`; `({}, ALREADY)` when
    there is nothing to do. Reads, never writes."""
    if not (root / PLUGIN_FILE).is_file():
        return {}, f"{PLUGIN_FILE.as_posix()} is not vendored here"
    ini, pyproject = root / "pytest.ini", root / "pyproject.toml"
    config = ini if ini.is_file() else pyproject
    if not config.is_file():
        return {}, "no pytest.ini or pyproject.toml at the root"
    text = _read(config)
    if LOAD in text:
        return {}, ALREADY
    if pinned := _pins_below_floor(root):
        return {}, f"{'; '.join(pinned)} -- recompile it at pytest>={FLOOR_TEXT} first"
    wired, why = wire_pytest_ini(text) if config == ini else wire_pyproject(text)
    if why:
        return {}, why
    edits = {config: wired}
    floors = [pyproject, *sorted(root.glob("requirements*.in"))]
    found = False
    for path in (p for p in floors if p.is_file()):
        raised, here = raise_floor(edits.get(path, _read(path)))
        found = found or here
        if raised != _read(path):
            edits[path] = raised
    if not found:
        return {}, "no `pytest>=` floor in pyproject.toml or requirements*.in to raise"
    return edits, ""


def _unquiet(value: str) -> str:
    return _QUIET.sub("", value).strip()


def drop_quiet_pyproject(text: str) -> str:
    """`text` with every quiet flag gone from `[tool.pytest.ini_options]`' one-line string
    `addopts`; `text` itself, byte for byte, when there is none to drop."""
    span = _span(text, _SECTION)
    found = _key_line(text[span[0] : span[1]], "addopts") if span else None
    single = found and re.fullmatch(r"(addopts[ \t]*=[ \t]*)([\"'])(.*)\2[ \t]*", found.group(0))
    if not span or not found or not single or not _QUIET.search(single.group(3)):
        return text
    head, quote, value = single.groups()
    start = span[0]
    line = f"{head}{quote}{_unquiet(value)}{quote}"
    return text[: start + found.start()] + line + text[start + found.end() :]


def drop_quiet_ini(text: str) -> str:
    """`pytest.ini`'s `[pytest]` with every quiet flag gone from `addopts`, continuation
    lines included and a line left empty by it dropped; `text` when there is none."""
    span = _span(text, _INI_SECTION)
    if span is None:
        return text
    start, end = span
    lines = text[start:end].splitlines(keepends=True)
    last = _value_end(lines, "addopts")
    if not last:
        return text
    first = next(i for i, line in enumerate(lines) if re.match(r"addopts[ \t]*=", line))
    kept = []
    for index in range(first, last):
        line = lines[index]
        body = line.rstrip("\n")
        head = _INI_HEAD.match(body)
        cut = head.end() if head else 0
        value = _unquiet(body[cut:])
        if not _QUIET.search(body[cut:]):
            kept.append(line)
        elif value or index == first:
            kept.append(f"{body[:cut]}{value}".rstrip() + line[len(body) :])
    if kept == lines[first:last]:
        return text
    return text[:start] + "".join([*lines[:first], *kept, *lines[last:]]) + text[end:]


def unquiet(root: Path) -> str:
    """`root`'s own pytest config with no quiet flag in `addopts`: `UNQUIETED` when one
    was dropped, `NOT_QUIET` when there was none (or no config) to drop."""
    ini, pyproject = root / "pytest.ini", root / "pyproject.toml"
    config = ini if ini.is_file() else pyproject
    if not config.is_file():
        return NOT_QUIET
    text = _read(config)
    new = drop_quiet_ini(text) if config == ini else drop_quiet_pyproject(text)
    if new == text:
        return NOT_QUIET
    _write(config, new, config.read_bytes())
    return UNQUIETED


def wire(root: Path, runner: Runner = sweep.run_windowless) -> str:
    """Wire `root` in place: `WIRED`, `ALREADY`, or why it was left as it was.

    Written only whole: a relock that fails restores every file, so a project is either
    wired and locked or exactly as it was."""
    edits, why = plan(root)
    if not edits:
        return why
    before = {path: path.read_bytes() for path in edits}
    for path, text in edits.items():
        _write(path, text, before[path])
    if (root / "pyproject.toml") in edits and (root / "uv.lock").is_file():
        uv = shutil.which("uv")
        done = (
            runner([uv, "lock"], cwd=str(root), capture_output=True, text=True, check=False)
            if uv
            else None
        )
        if done is None or done.returncode != 0:
            for path, raw in before.items():
                path.write_bytes(raw)
            said = "uv is not on PATH" if done is None else (done.stderr or done.stdout).strip()
            return f"left unwired: `uv lock` failed -- {said[-300:]}"
    return WIRED


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0] if args else ".").resolve()
    said = wire(root)
    print(f"temproot-wiring: {root.name}: {said}; {unquiet(root)}")
    return 0 if said in (WIRED, ALREADY) else 1


if __name__ == "__main__":
    raise SystemExit(main())
