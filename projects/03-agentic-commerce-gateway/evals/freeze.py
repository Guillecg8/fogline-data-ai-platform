"""Freeze everything a held-out result depends on (ADR 0006, ADR 0007).

    uv run poe cg-freeze

Writes `results/freeze/freeze-v<N>.json`, the next version; an existing version is never
overwritten or removed. A version after the first needs a "freeze v<N>" entry in
`results/CHANGELOG.md` saying what changed. The runner refuses a held-out batch unless the
current values equal the latest version.
"""

import json
import os
import re
import sys
import tempfile
from pathlib import Path

import anyio
import oracle
from agent.model import OllamaModel, Options
from agent.runner import (
    AGENT_DIR,
    CATALOGUE,
    MANDATES,
    ORACLE_FILE,
    PROJECT,
    REPO,
    SYSTEM_PROMPT_FILE,
    TASKS_FILE,
    _ollama_tools,
    git_tree_hash,
    text_hash,
    tools_hash,
)
from mcp import ClientSession, StdioServerParameters, stdio_client

EVALS = Path(__file__).resolve().parent
FREEZE_DIR = EVALS / "results" / "freeze"
CHANGELOG = EVALS / "results" / "CHANGELOG.md"
CODE_FILES = {
    "runner": AGENT_DIR / "runner.py",
    "model": AGENT_DIR / "model.py",
    "classifier": AGENT_DIR / "classify.py",
    "approver": AGENT_DIR / "approver.py",
    "summary": EVALS / "agent_summary.py",
    "batch": EVALS / "run_agents.py",
    "freeze": EVALS / "freeze.py",
}
_VERSION = re.compile(r"freeze-v([0-9]+)\.json")


def _repo_path(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


async def _listed_tools(mandate: Path, db: Path) -> str:
    """Hash of the tool contract as the server lists it: names, descriptions, schemas and the
    instructions text."""
    env = {**os.environ, "COMMERCE_GATEWAY_MANDATE": str(mandate), "COMMERCE_GATEWAY_DB": str(db)}
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "commerce_gateway.server"], env=env
    )
    with open(os.devnull, "w", encoding="utf-8") as err:
        async with stdio_client(params, errlog=err) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                tools = (await session.list_tools()).tools
    return tools_hash(_ollama_tools(tools), init.instructions)


def current_values(model_identity: dict) -> dict:
    """Everything the freeze pins, computed now."""
    with tempfile.TemporaryDirectory(prefix="cg-freeze-") as workdir:
        tools = anyio.run(_listed_tools, MANDATES / "standard.json", Path(workdir) / "f.db")
    evals = _repo_path(EVALS)
    return {
        "tasks": text_hash(TASKS_FILE),
        "system_prompt": text_hash(SYSTEM_PROMPT_FILE),
        "tool_contract": tools,
        "oracle": text_hash(ORACLE_FILE),
        "catalogue": text_hash(CATALOGUE),
        "mandates": {path.name: text_hash(path) for path in sorted(MANDATES.glob("*.json"))},
        "code": {name: text_hash(path) for name, path in CODE_FILES.items()},
        "git_trees": {
            "src": git_tree_hash(_repo_path(PROJECT / "src")),
            # results/ holds the freeze, the change log and the run outputs: excluded.
            "evals": git_tree_hash(evals, exclude=(f"{evals}/results/",)),
        },
        "model": model_identity,
    }


def versions(directory: Path = FREEZE_DIR) -> list[int]:
    if not directory.exists():
        return []
    found = (_VERSION.fullmatch(path.name) for path in directory.iterdir())
    return sorted(int(match.group(1)) for match in found if match)


def latest(directory: Path = FREEZE_DIR) -> dict:
    numbers = versions(directory)
    if not numbers:
        raise RuntimeError("no freeze: run `uv run poe cg-freeze` before a held-out batch")
    path = directory / f"freeze-v{numbers[-1]}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _changed(current: dict, frozen: dict, prefix: str = "") -> list[str]:
    keys = sorted(set(current) | set(frozen))
    found = []
    for key in keys:
        a, b = current.get(key), frozen.get(key)
        if isinstance(a, dict) and isinstance(b, dict):
            found += _changed(a, b, f"{prefix}{key}.")
        elif a != b:
            found.append(prefix + key)
    return found


def check_freeze(model_identity: dict, directory: Path = FREEZE_DIR) -> str:
    """The latest freeze's hash if every current value equals it; raises naming what differs."""
    frozen = latest(directory)
    current = current_values(model_identity)
    if None in (current["git_trees"]["src"], current["git_trees"]["evals"]):
        raise RuntimeError("the git trees cannot be read; a held-out run needs git")
    changed = _changed(current, frozen["values"])
    if changed:
        raise RuntimeError(
            f"the freeze v{frozen['version']} does not match: {', '.join(changed)} changed"
        )
    return frozen["freeze_hash"]


def write_freeze(
    model_identity: dict, directory: Path = FREEZE_DIR, changelog: Path = CHANGELOG
) -> dict:
    """Write the next version. Versions after the first need a CHANGELOG entry."""
    numbers = versions(directory)
    version = (numbers[-1] if numbers else 0) + 1
    if version > 1:
        text = changelog.read_text(encoding="utf-8") if changelog.exists() else ""
        if f"freeze v{version}" not in text:
            raise RuntimeError(
                f"write a 'freeze v{version}' entry in {changelog.name} saying what changed"
            )
    values = current_values(model_identity)
    document = {"version": version, "values": values, "freeze_hash": oracle.sha256_hex(values)}
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"freeze-v{version}.json"
    with path.open("x", encoding="utf-8") as handle:  # "x": never overwrite
        handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return document


def main() -> int:
    identity = OllamaModel(Options()).identity()
    if not identity.get("digest"):
        print("the model is not available from Ollama; nothing was frozen", file=sys.stderr)
        return 1
    try:
        document = write_freeze(identity)
    except RuntimeError as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 2
    print(f"froze v{document['version']}: {document['freeze_hash']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
