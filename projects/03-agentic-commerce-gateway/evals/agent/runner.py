"""The agent loop (ADR 0007): one task, one model, the real MCP server over stdio.

The tools are passed to the model exactly as the server lists them, with the server's
instructions appended to the fixed system prompt. Each model call is one step; each tool call
goes through the SDK's ClientSession unchanged. After any `pending_approval` result the simulated
approver acts through the real CLI before the next step. The oracle judges the final database.
"""

import hashlib
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import anyio
import oracle
from mcp import ClientSession, StdioServerParameters, stdio_client

from agent import approver, classify
from agent.model import Model, ModelReply, Options

AGENT_DIR = Path(__file__).resolve().parent
PROJECT = AGENT_DIR.parents[1]
REPO = PROJECT.parents[1]
CATALOGUE = PROJECT / "src" / "commerce_gateway" / "fixtures" / "catalogue_v1.json"
TASKS_FILE = AGENT_DIR / "tasks.json"
SYSTEM_PROMPT_FILE = AGENT_DIR / "system_prompt.txt"
MANDATES = AGENT_DIR / "mandates"
PERMISSIVE_MANDATE = MANDATES / "permissive.json"
ORACLE_FILE = AGENT_DIR.parent / "oracle.py"

MAIN = "main"
CONTROLS_OFF = "controls-off"
CONTENT_CAP = 2000
ARGUMENTS_CAP = 2000
CONTEXT_LIMIT = 0.9  # a step at 90 % of num_ctx makes the run invalid
_TEXT_TOOL_CALL = re.compile(r"<tool_call>|\"name\"\s*:\s*\"[a-z_]+\"\s*,\s*\"arguments\"")


@dataclass(frozen=True)
class Limits:
    max_steps: int = 24
    max_tool_calls: int = 40
    max_generated_tokens: int = 8000
    timeout_s: float = 30 * 60


# --- Hashes and the run's identity ------------------------------------------------------------


def text_hash(path: Path) -> str:
    """SHA-256 of a text file with line endings normalised, so a CRLF checkout hashes the same."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tools_hash(tools: list[dict], instructions: str | None) -> str:
    return oracle.sha256_hex({"tools": tools, "instructions": instructions})


def load_tasks() -> list[dict]:
    return json.loads(TASKS_FILE.read_text(encoding="utf-8"))["tasks"]


def git_state(repo: Path = REPO) -> dict:
    """The commit and whether the tree is dirty, read-only; None where git cannot tell."""

    def run(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args], cwd=repo, capture_output=True, encoding="utf-8", timeout=30
            )
        except OSError:
            return None
        # Only trailing newlines are removed: porcelain lines start with a meaningful space.
        return result.stdout.rstrip("\n") if result.returncode == 0 else None

    status = run("status", "--porcelain", "--untracked-files=all")
    dirty = bool(status) if status is not None else None
    changed = None if status is None else [line[3:] for line in status.splitlines() if line]
    return {"commit": run("rev-parse", "HEAD"), "dirty": dirty, "changed_paths": changed}


def git_tree_hash(path: str, exclude: tuple[str, ...] = (), repo: Path = REPO) -> str | None:
    """SHA-256 of `git ls-tree -r HEAD -- path` (mode, type, blob hash, path per file), minus
    paths under `exclude`. It pins the committed content of a directory; read-only."""
    try:
        result = subprocess.run(
            ["git", "ls-tree", "-r", "HEAD", "--", path],
            cwd=repo,
            capture_output=True,
            encoding="utf-8",
            timeout=30,
        )
    except OSError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    lines = [
        line
        for line in result.stdout.splitlines()
        if not any(line.split("\t", 1)[1].startswith(prefix) for prefix in exclude)
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _cpu() -> str:
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        for line in cpuinfo.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    return platform.processor() or platform.machine()


def _categories() -> dict[str, str]:
    document = json.loads(CATALOGUE.read_text(encoding="utf-8"))
    return {product["sku"]: product["policy_category"] for product in document["products"]}


# --- The run ----------------------------------------------------------------------------------


def _ollama_tools(tools: list) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }
        for tool in tools
    ]


def _capped_json(value: object, cap: int) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=repr)
    return text if len(text) <= cap else text[:cap] + "...[truncated]"


class _Session:
    """State of one run while it is in progress."""

    def __init__(self, task: dict, model: Model, options: Options, limits: Limits) -> None:
        self.task = task
        self.model = model
        self.options = options
        self.limits = limits
        self.steps: list[dict] = []
        self.calls: list[dict] = []
        self.approver_actions: list[dict] = []
        self.invalid: list[str] = []
        self.decided: set[int] = set()
        self.cart_total: int | None = None
        self.generated = 0
        self.final_answer: str | None = None
        self.stopped_by = "answer"


async def _call_tool(session: ClientSession, name: object, arguments: object) -> dict:
    """One tool call; returns the structured result or a transport error, never raises."""
    try:
        result = await session.call_tool(name, arguments)  # type: ignore[arg-type]
    except Exception as exc:  # the SDK refused to send it (non-object arguments) or failed
        return {"transport_error": type(exc).__name__}
    body = result.structured_content
    text = result.content[0].text if result.content else json.dumps(body)
    return {"body": body, "text": text}


async def _loop(
    state: _Session,
    session: ClientSession,
    system_prompt: str,
    tools: list[dict],
    env: dict[str, str],
    approver_mode: str,
) -> None:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": state.task["instruction"]},
    ]
    started = time.monotonic()
    num_ctx = state.options.num_ctx
    while True:
        if len(state.steps) >= state.limits.max_steps:
            state.stopped_by = "step_cap"
            return
        if time.monotonic() - started > state.limits.timeout_s:
            state.stopped_by = "timeout"
            return
        reply: ModelReply = await anyio.to_thread.run_sync(state.model.chat, messages, tools)
        state.generated += reply.eval_count
        step = {
            "index": len(state.steps) + 1,
            "prompt_eval_count": reply.prompt_eval_count,
            "prompt_eval_cached_count": reply.prompt_eval_cached_count,
            "eval_count": reply.eval_count,
            "done_reason": reply.done_reason,
            "durations_ms": reply.durations_ms,
            "content": reply.content[:CONTENT_CAP],
            "tool_call_in_text": bool(_TEXT_TOOL_CALL.search(reply.content)),
            "tool_calls": [],
        }
        state.steps.append(step)
        if reply.prompt_eval_count >= CONTEXT_LIMIT * num_ctx:
            state.invalid.append(f"context: step {step['index']} read {reply.prompt_eval_count}")
        if reply.done_reason == "length":
            state.invalid.append(f"length: step {step['index']} hit num_predict")
        messages.append(reply.raw_message or {"role": "assistant", "content": reply.content})
        if not reply.tool_calls:
            state.final_answer = reply.content[:CONTENT_CAP]
            return
        for tool_call in reply.tool_calls:
            if len(state.calls) >= state.limits.max_tool_calls:
                state.stopped_by = "tool_call_cap"
                return
            state.calls.append(
                {
                    "step": step["index"],
                    "name": tool_call.name,
                    "arguments": tool_call.arguments,
                    "cart_total_before": state.cart_total,
                }
            )
            result = await _call_tool(session, tool_call.name, tool_call.arguments)
            body = result.get("body") or {}
            data = body.get("data") or {}
            if isinstance(data.get("cart"), dict):
                state.cart_total = data["cart"].get("total_cents")
            step["tool_calls"].append(
                {
                    "name": tool_call.name,
                    "arguments": _capped_json(tool_call.arguments, ARGUMENTS_CAP),
                    "outcome": body.get("outcome"),
                    "rule_id": body.get("rule_id"),
                    "error": body.get("error"),
                    "transport_error": result.get("transport_error"),
                }
            )
            content = result.get("text") or json.dumps(
                {"error": "the call could not be sent", "kind": result.get("transport_error")}
            )
            messages.append({"role": "tool", "content": content, "tool_name": tool_call.name})
            checkout = data.get("checkout") if isinstance(data.get("checkout"), dict) else {}
            if body.get("outcome") == "pending_approval" and checkout.get("checkout_id") not in (
                state.decided
            ):
                state.decided.add(checkout.get("checkout_id"))
                intended = tuple(tuple(line) for line in state.task["intended_lines"])
                actions = await anyio.to_thread.run_sync(approver.act, env, approver_mode, intended)
                state.approver_actions.extend(actions)
        if state.generated >= state.limits.max_generated_tokens:
            state.stopped_by = "token_cap"
            return


async def _run_async(
    state: _Session, env: dict[str, str], errlog: Path, approver_mode: str
) -> dict:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "commerce_gateway.server"], env=env
    )
    with errlog.open("a", encoding="utf-8") as err:
        async with stdio_client(params, errlog=err) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                listed = (await session.list_tools()).tools
                tools = _ollama_tools(listed)
                prompt = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").strip()
                system_prompt = f"{prompt}\n\n{init.instructions}" if init.instructions else prompt
                await _loop(state, session, system_prompt, tools, env, approver_mode)
    return {
        "tool_names": tuple(tool.name for tool in listed),
        "tools_hash": tools_hash(tools, init.instructions),
    }


def _anchor(db: Path) -> list:
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT seq, row_hash FROM audit ORDER BY seq DESC LIMIT 1").fetchall()
    finally:
        conn.close()
    return list(rows[0]) if rows else [0, oracle.GENESIS]


def run_task(
    task: dict,
    model: Model,
    options: Options,
    workdir: Path,
    arm: str = MAIN,
    approver_mode: str = approver.ATTENTIVE,
    repetition: int = 0,
    limits: Limits | None = None,
) -> dict:
    """Run one task end to end and return its record (ADR 0007)."""
    workdir.mkdir(parents=True, exist_ok=False)
    db = workdir / "gateway.db"
    strict_mandate = MANDATES / task["mandate"]
    server_mandate = PERMISSIVE_MANDATE if arm == CONTROLS_OFF else strict_mandate
    env = {
        **os.environ,
        "COMMERCE_GATEWAY_MANDATE": str(server_mandate),
        "COMMERCE_GATEWAY_DB": str(db),
    }
    state = _Session(task, model, options, limits or Limits())
    started = time.monotonic()
    served = anyio.run(_run_async, state, env, workdir / "server-stderr.txt", approver_mode)
    wall_ms = int((time.monotonic() - started) * 1000)

    judged = oracle.check(db, server_mandate, CATALOGUE)
    oracle_result = {"violations": judged.by_check()}
    ok = judged.ok
    if arm == CONTROLS_OFF:
        strict = oracle.check(db, strict_mandate, CATALOGUE).by_check()
        strict.pop("mandate_hash", None)  # expected: the snapshot is the permissive mandate
        oracle_result = {
            "permissive": judged.by_check(),
            "strict_excluding_mandate_hash": strict,
            "note": "controls-off: strict-mandate violations are expected, not bugs",
        }

    completed = classify.final_lines(db)
    pending = classify.pending_count(db)
    valid = not state.invalid
    gateway = classify.audit_counts(db)
    transcript = classify.transcript_attempts(
        task, state.calls, served["tool_names"], _categories()
    )
    unexpected = sum(judged.by_check().values()) if arm == MAIN else 0
    gateway_attempts = sum(gateway["blocks"].values()) + unexpected
    metrics = {
        "success": classify.succeeded(task, completed, pending, valid, ok),
        "valid": valid,
        "completed_lines": classify.as_lines(completed),
        "pending_checkouts": pending,
        "permitted_deviations": classify.deviations(task, db) if ok else None,
        "attempts_transcript": len(transcript),
        "attempts_gateway": gateway_attempts,
        "attempts_difference": len(transcript) - gateway_attempts,
        "transcript_attempts": transcript,
        **gateway,
        "completed_forbidden_actions": unexpected,
        "steps": len(state.steps),
        "tool_calls": len(state.calls),
        "tool_calls_in_text": sum(1 for step in state.steps if step["tool_call_in_text"]),
        "prompt_tokens": sum(step["prompt_eval_count"] for step in state.steps),
        "generated_tokens": state.generated,
        "peak_prompt_eval_count": max((s["prompt_eval_count"] for s in state.steps), default=0),
        "wall_ms": wall_ms,
    }
    identity = model.identity() if hasattr(model, "identity") else {"model": model.name}
    return {
        "task_id": task["id"],
        "type": task["type"],
        "split": task["split"],
        "arm": arm,
        "approver_mode": approver_mode,
        "repetition": repetition,
        "options": asdict(options),
        "limits": asdict(state.limits),
        "model": identity,
        "host_cpu": _cpu(),
        "git": git_state(),
        "hashes": {
            "tasks": text_hash(TASKS_FILE),
            "system_prompt": text_hash(SYSTEM_PROMPT_FILE),
            "tools": served["tools_hash"],
            "mandate_server": text_hash(server_mandate),
            "mandate_strict": text_hash(strict_mandate),
            "catalogue": text_hash(CATALOGUE),
            "oracle": text_hash(ORACLE_FILE),
        },
        "stopped_by": state.stopped_by,
        "invalid": state.invalid,
        "final_answer": state.final_answer,
        "steps": state.steps,
        "approver_actions": state.approver_actions,
        "audit_anchor": _anchor(db),
        "oracle": oracle_result,
        "metrics": metrics,
    }
