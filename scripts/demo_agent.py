"""Drive a fixed damaged-order ticket through an already running application API."""

import argparse
import json
import math
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx

from tool_agent_lab.agent.inputs import InputRequest
from tool_agent_lab.schemas.actions import ApprovalDecision, ApprovalRequest
from tool_agent_lab.schemas.tasks import TaskCreate


def demo_agent(
    client: httpx.Client, *, decision: str | None = None, timeout: float = 60,
    poll_interval: float = 0.25,
) -> dict:
    """Submit one example; human decisions use the exact proposal returned by the API."""
    selected = ApprovalDecision(decision) if decision is not None else None

    def request(method: str, path: str, body: dict | None = None):
        response = client.request(method, path, **({"json": body} if body is not None else {}))
        response.raise_for_status()
        return response.json()

    started = time.monotonic()
    deadline = started + timeout
    task = request("POST", "/tasks", TaskCreate(
        user_message="商品到货损坏，请先询问订单号，再核对政策并提出退款方案。",
    ).model_dump(mode="json", exclude_none=True))
    path = f"/tasks/{task['task_id']}"
    print(json.dumps({"task_id": task["task_id"], "api_url": str(client.base_url)}, ensure_ascii=False),
          file=sys.stderr, flush=True)
    report = {"workflow": "api_agent", "scenario": "damaged_refund", "api_url": str(client.base_url),
              "amount_unit": "fen", "inputs": [], "approvals": [], "states": []}
    human_wait = 0.0

    def finish(reason: str, detail: dict) -> dict:
        attempt = detail["attempt"]
        return report | {"finish_reason": reason, "task_id": detail["task_id"],
                         "thread_id": attempt["thread_id"], "model_version": attempt["model_version"],
                         "config_version": attempt["config_version"], "task": detail,
                         "elapsed_seconds": round(time.monotonic() - started, 3),
                         "human_wait_seconds": round(human_wait, 3)}

    while True:
        detail = request("GET", path)
        status = detail["status"]
        if not report["states"] or report["states"][-1] != status:
            report["states"].append(status)
        if status == "completed" and detail["result"] is not None:
            return finish("completed", detail)
        if status in ("failed", "cancelled"):
            return finish(status, detail)
        if time.monotonic() >= deadline:
            return finish("timeout", detail)
        if status == "waiting_input":
            if report["inputs"]:
                return finish("additional_input_required", detail)
            prompt = detail["input_request"]
            reply = InputRequest(request_id=f"demo-input-{uuid4().hex}",
                                 input_request_id=prompt["request_id"], message="ORD-1001")
            receipt = request("POST", path + "/input", reply.model_dump(mode="json"))
            report["inputs"].append({"prompt": prompt, "receipt": receipt})
        elif status == "waiting_approval":
            proposal = request("GET", path + "/proposal")
            print(json.dumps({"proposal": proposal, "model_version": detail["attempt"]["model_version"]},
                             ensure_ascii=False, indent=2), file=sys.stderr, flush=True)
            if report["approvals"]:
                return finish("additional_approval_required", detail)
            if selected is None:
                print("Enter approved or rejected for this proposal:", file=sys.stderr, flush=True)
                waiting_since = time.monotonic()
                answer = sys.stdin.readline()
                waited = time.monotonic() - waiting_since
                human_wait += waited
                deadline += waited
                if not answer:
                    return finish("operator_input_missing", detail)
                selected = ApprovalDecision(answer.strip())
            approval = ApprovalRequest(request_id=f"demo-approval-{uuid4().hex}",
                                       proposal_id=proposal["proposal_id"],
                                       proposal_version=proposal["proposal_version"], decision=selected)
            receipt = request("POST", path + "/approval", approval.model_dump(mode="json"))
            report["approvals"].append({"proposal": proposal, "receipt": receipt})
        time.sleep(poll_interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Application API, not a model endpoint")
    parser.add_argument("--decision", choices=[value.value for value in ApprovalDecision],
                        help="Explicit decision for the first proposal; otherwise read operator input")
    parser.add_argument("--timeout", type=float, default=60, help="Polling budget in seconds; operator wait is excluded")
    parser.add_argument("--output", type=Path, help="Save the UTF-8 JSON receipt, relative to the current directory")
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        with httpx.Client(base_url=args.base_url, timeout=10) as client:
            report = demo_agent(client, decision=args.decision, timeout=args.timeout)
        text = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text + "\n", encoding="utf-8")
        print(text)
    except (httpx.HTTPError, OSError, ValueError) as error:
        print(f"Agent demonstration stopped: {error}", file=sys.stderr)
        return 1
    return 0 if report["finish_reason"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
