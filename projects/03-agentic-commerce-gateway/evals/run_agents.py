"""Run a batch of agent tasks against the local model (ADR 0007). Run by hand, never in CI.

    uv run python projects/03-agentic-commerce-gateway/evals/run_agents.py \\
        --split dev --tasks T01,T13,T23 --label pilot

Each batch writes `results/agent-runs/<batch_id>/` (never overwritten): one record per run and a
`batch.json`. Databases go to the git-ignored `data/p3-agent-runs/<batch_id>/`.
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

from agent import approver
from agent.model import OllamaModel, Options
from agent.runner import CONTROLS_OFF, MAIN, REPO, git_state, load_tasks, run_task
from freeze import check_freeze

RESULTS = Path(__file__).resolve().parent / "results" / "agent-runs"
RUN_OUTPUTS = RESULTS.relative_to(REPO).as_posix() + "/"
DATA = REPO / "data" / "p3-agent-runs"


def held_out_preconditions(identity: dict, git: dict | None = None) -> str:
    """A held-out run needs a clean git tree and a matching freeze; returns the freeze hash.

    Dev runs may be dirty; the record keeps the flag either way.
    """
    git = git if git is not None else git_state()
    changed = git.get("changed_paths")
    if git.get("commit") is None or changed is None:
        raise RuntimeError("the git tree is dirty or unknown; commit before a held-out run")
    # Earlier batches' outputs are the only changes allowed: a batch never edits anything else.
    stray = [path for path in changed if not path.startswith(RUN_OUTPUTS)]
    if stray:
        raise RuntimeError(
            f"the git tree is dirty ({len(stray)} paths); commit before a held-out run"
        )
    return check_freeze(identity)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--split", choices=("dev", "held_out"), required=True)
    parser.add_argument("--tasks", help="comma-separated task ids (default: the whole split)")
    parser.add_argument("--arm", choices=(MAIN, CONTROLS_OFF), default=MAIN)
    parser.add_argument("--approver", choices=approver.MODES, default=approver.ATTENTIVE)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--label", default="run", help="a short word for the batch id")
    return parser


def main() -> int:
    args = _parser().parse_args()
    tasks = [task for task in load_tasks() if task["split"] == args.split]
    if args.tasks:
        wanted = args.tasks.split(",")
        tasks = [task for task in tasks if task["id"] in wanted]
        if len(tasks) != len(wanted):
            print("some task ids are not in that split", file=sys.stderr)
            return 2
    if args.arm == CONTROLS_OFF:
        tasks = [task for task in tasks if task["adversarial"]]
    model_options = Options(temperature=args.temperature, seed=args.seed)
    model = OllamaModel(model_options)
    identity = model.identity()
    try:
        freeze = held_out_preconditions(identity) if args.split == "held_out" else None
    except RuntimeError as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 3
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    batch_id = f"{stamp}-{args.label}-{args.split}-{args.arm}-{args.approver}"
    out, data = RESULTS / batch_id, DATA / batch_id
    out.mkdir(parents=True, exist_ok=False)  # a batch never overwrites another
    summaries = []
    for task in tasks:
        for repetition in range(args.repetitions):
            options = Options(temperature=args.temperature, seed=args.seed + repetition)
            model.options = options
            record = run_task(
                task,
                model,
                options,
                data / f"{task['id']}-r{repetition}",
                args.arm,
                args.approver,
                repetition,
            )
            record["freeze_hash"] = freeze
            name = f"{task['id']}-r{repetition}.json"
            (out / name).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
            metrics = record["metrics"]
            brief = {key: metrics[key] for key in ("success", "valid", "steps")}
            summaries.append({"run": name, **brief})
            print(
                f"{task['id']} r{repetition}: success={metrics['success']} steps={metrics['steps']}"
                f" tokens={metrics['prompt_tokens']}+{metrics['generated_tokens']}"
                f" peak={metrics['peak_prompt_eval_count']} {metrics['wall_ms'] / 60000:.1f} min",
                flush=True,
            )
    batch = {
        "batch_id": batch_id,
        "args": vars(args),
        "model": identity,
        "freeze_hash": freeze,
        "runs": summaries,
    }
    (out / "batch.json").write_text(json.dumps(batch, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
