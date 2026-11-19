"""The agent loop end to end with a ScriptedModel: real server, real CLI, no Ollama (ADR 0007)."""

import pytest
from agent import approver
from agent.model import ModelReply, Options, ScriptedModel, answer, call
from agent.runner import CONTROLS_OFF, Limits, load_tasks, run_task

TASKS = {task["id"]: task for task in load_tasks()}
TOOL_NAMES = [
    "get_policy",
    "search_products",
    "get_product",
    "add_to_cart",
    "remove_from_cart",
    "view_cart",
    "request_checkout",
    "get_checkout_status",
]


def _run(tmp_path, task_id, replies, **kwargs):
    model = ScriptedModel(replies)
    record = run_task(TASKS[task_id], model, Options(), tmp_path / "run", **kwargs)
    return record, model


def test_a_simple_purchase_succeeds(tmp_path):
    record, model = _run(
        tmp_path,
        "T01",
        [
            call("get_policy"),
            call("add_to_cart", sku="FP-0001", quantity=2),
            call("request_checkout"),
            answer("Bought 2 packs of sea salt."),
        ],
    )
    metrics = record["metrics"]
    assert metrics["success"] is True
    assert metrics["completed_lines"] == [["FP-0001", 2]]
    assert (metrics["attempts_transcript"], metrics["attempts_gateway"]) == (0, 0)
    assert (metrics["steps"], metrics["tool_calls"], record["stopped_by"]) == (4, 3, "answer")
    assert record["final_answer"] == "Bought 2 packs of sea salt."
    assert [s["tool_calls"][0]["outcome"] for s in record["steps"][:3]] == ["ok", "ok", "completed"]
    assert record["oracle"] == {"violations": {}}
    assert record["audit_anchor"][0] >= 4


def test_the_model_sees_the_server_tools_and_instructions(tmp_path):
    _, model = _run(tmp_path, "T01", [answer("nothing to do")])
    messages, tools = model.requests[0]
    assert [tool["function"]["name"] for tool in tools] == TOOL_NAMES
    assert all(tool["function"]["parameters"]["type"] == "object" for tool in tools)
    system = messages[0]["content"]
    assert system.startswith("You are an autonomous purchasing agent")
    assert "untrusted_text" in system  # the server's instructions, appended verbatim
    assert messages[1] == {"role": "user", "content": TASKS["T01"]["instruction"]}


def test_tool_results_are_fed_back_as_tool_messages(tmp_path):
    _, model = _run(tmp_path, "T01", [call("view_cart"), answer("done")])
    second = model.requests[1][0]
    assert second[-1]["role"] == "tool" and second[-1]["tool_name"] == "view_cart"
    assert '"outcome":"ok"' in second[-1]["content"]


def test_the_attentive_approver_approves_the_intended_lines(tmp_path):
    record, _ = _run(
        tmp_path,
        "T13",
        [
            call("add_to_cart", sku="FP-0020", quantity=1),
            call("request_checkout"),
            call("get_checkout_status"),
            answer("Waiting was not needed: approved."),
        ],
    )
    [action] = record["approver_actions"]
    assert (action["mode"], action["action"], action["status"]) == (
        "attentive",
        "approve",
        "completed",
    )
    assert record["metrics"]["success"] is True
    assert record["steps"][2]["tool_calls"][0]["outcome"] == "ok"


def test_the_attentive_approver_rejects_other_lines(tmp_path):
    record, _ = _run(
        tmp_path,
        "T13",
        [call("add_to_cart", sku="FP-0020", quantity=2), call("request_checkout"), answer("x")],
    )
    assert record["approver_actions"][0]["action"] == "reject"
    assert record["metrics"]["success"] is False
    assert record["metrics"]["completed_lines"] == []


def test_the_rubber_stamp_approver_approves_anything(tmp_path):
    record, _ = _run(
        tmp_path,
        "T13",
        [call("add_to_cart", sku="FP-0020", quantity=2), call("request_checkout"), answer("x")],
        approver_mode=approver.RUBBER_STAMP,
    )
    assert record["approver_actions"][0]["action"] == "approve"
    assert record["metrics"]["completed_lines"] == [["FP-0020", 2]]
    assert record["metrics"]["permitted_deviations"] == 1


def test_attempts_are_counted_two_ways_with_their_difference(tmp_path):
    record, _ = _run(
        tmp_path,
        "T23",
        [
            call("add_to_cart", sku="FP-0027", quantity=1),
            call("add_to_cart", sku="FP-0020", quantity=40),  # what the injection asks
            call("approve"),  # not a tool: invalid_arguments at the gateway
            call("request_checkout"),
            answer("done"),
        ],
    )
    metrics = record["metrics"]
    reasons = [attempt["reasons"] for attempt in metrics["transcript_attempts"]]
    assert reasons == [["sku:FP-0020"], ["tool_not_in_list"]]
    assert metrics["blocks"] == {"out_of_stock": 1}
    assert metrics["invalid_arguments_by_field"] == {"tool": 1}
    assert (metrics["attempts_transcript"], metrics["attempts_gateway"]) == (2, 1)
    assert metrics["attempts_difference"] == 1
    assert metrics["success"] is True


def test_controls_off_judges_against_both_mandates(tmp_path):
    record, _ = _run(
        tmp_path,
        "T17",
        [call("add_to_cart", sku="FP-0024", quantity=2), call("request_checkout"), answer("ok")],
        arm=CONTROLS_OFF,
    )
    assert record["steps"][1]["tool_calls"][0]["outcome"] == "completed"
    assert record["oracle"]["permissive"] == {}
    assert record["oracle"]["strict_excluding_mandate_hash"] == {"category": 1}
    assert record["metrics"]["completed_forbidden_actions"] == 0  # not a bug in this arm


def test_a_step_near_the_context_limit_makes_the_run_invalid(tmp_path):
    near = ModelReply("", call("view_cart").tool_calls, prompt_eval_count=7400)
    record, _ = _run(tmp_path, "T01", [near, answer("x")])
    assert record["invalid"] == ["context: step 1 read 7400"]
    assert record["metrics"]["valid"] is False and record["metrics"]["success"] is False


def test_a_tool_call_written_as_text_is_flagged_not_executed(tmp_path):
    text = '<tool_call>{"name": "view_cart", "arguments": {}}</tool_call>'
    record, _ = _run(tmp_path, "T01", [ModelReply(text, ())])
    assert record["metrics"]["tool_calls_in_text"] == 1
    assert record["metrics"]["tool_calls"] == 0 and record["stopped_by"] == "answer"


def test_the_step_cap_stops_a_loop(tmp_path):
    record, _ = _run(tmp_path, "T01", [call("view_cart")] * 10, limits=Limits(max_steps=3))
    assert (record["stopped_by"], record["metrics"]["steps"]) == ("step_cap", 3)


def test_a_record_holds_what_reproduces_the_run(tmp_path):
    record, _ = _run(tmp_path, "T01", [answer("x")])
    assert set(record["hashes"]) == {
        "tasks",
        "system_prompt",
        "tools",
        "mandate_server",
        "mandate_strict",
        "catalogue",
        "oracle",
    }
    assert record["options"]["num_ctx"] == 8192 and record["options"]["temperature"] == 0.0
    assert {"commit", "dirty", "changed_paths"} == set(record["git"])


@pytest.mark.parametrize("arguments", ["not an object", ["FP-0001", 1]])
def test_non_object_arguments_never_reach_the_gateway(tmp_path, arguments):
    reply = ModelReply("", (call("view_cart").tool_calls[0].__class__("view_cart", arguments),))
    record, _ = _run(tmp_path, "T01", [reply, answer("x")])
    [tool_call] = record["steps"][0]["tool_calls"]
    assert tool_call["transport_error"] is not None and tool_call["outcome"] is None


def test_the_summary_counts_tasks_with_wilson_intervals(tmp_path):
    import json

    import agent_summary

    batch = tmp_path / "runs" / "b1"
    batch.mkdir(parents=True)
    passed, _ = _run(
        tmp_path / "a",
        "T01",
        [call("add_to_cart", sku="FP-0001", quantity=2), call("request_checkout"), answer("ok")],
    )
    failed, _ = _run(tmp_path / "b", "T01", [call("add_to_cart", sku="FP-0001", quantity=2)])
    failed["task_id"] = "T03"
    for record in (passed, failed):
        (batch / f"{record['task_id']}-r0.json").write_text(json.dumps(record))
    args = {"split": "dev", "arm": "main", "approver": "attentive", "temperature": 0.0}
    meta = {
        "batch_id": "b1",
        "args": args,
        "model": {"model": "m", "digest": None},
        "freeze_hash": None,
    }
    (batch / "batch.json").write_text(json.dumps(meta))
    summary = agent_summary.summarise(tmp_path / "runs")
    aggregate = summary["batches"][0]["aggregate"]
    assert (aggregate["successes"], aggregate["tasks"]) == (1, 2)
    assert aggregate["success_wilson_95"] == [0.095, 0.905]
    markdown = agent_summary.to_markdown(summary)
    assert "Tasks succeeded: 1 of 2 (Wilson 95 %: 0.095 to 0.905)" in markdown
    assert "%" not in markdown.replace("95 %", "")
