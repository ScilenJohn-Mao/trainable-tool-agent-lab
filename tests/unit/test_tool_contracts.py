"""Validate tool schemas and structured records without transport or business execution."""

import hashlib
import json
import subprocess
import sys

import pytest
from pydantic import ValidationError

from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.schemas.actions import CouponAction, HandoffAction, RefundAction
from tool_agent_lab.schemas.business import Operation, Order
from tool_agent_lab.settings import PROJECT_ROOT
from tool_agent_lab.tools.contracts import (
    TOOL_CONTRACTS, OperationLookup, PolicyDocument, PolicyHit, PolicySearchResult,
    ToolError, ToolFailure, ToolName, UnknownExecutionError, tool_definitions,
)


@pytest.fixture
def samples() -> dict:
    data = PROJECT_ROOT / "data/business/v1"
    rules = BusinessRules.from_file(data / "spec.json")
    reference = rules.reference("request_refund")
    refund = RefundAction(order_id="ORD-1001", amount_minor=12900, reason="verified damage", policy_refs=(reference,))
    coupon = CouponAction(order_id="ORD-1002", amount_minor=500, reason="delayed delivery", policy_refs=(rules.reference("issue_coupon"),))
    handoff = HandoffAction(order_id="ORD-1004", reason="unsupported_category", summary="manual review",
                            policy_refs=(rules.reference("create_handoff"),))
    order = Order.model_validate(json.loads((data / "orders.json").read_text(encoding="utf-8"))[0])
    operation = Operation(operation_key="refund-key", task_id="task", attempt_id="attempt", owner_id="demo-user",
                          order_id=refund.order_id, action=refund.action, amount_minor=refund.amount_minor,
                          approval_request_id="approval", status="succeeded", created_at=rules.business_time,
                          committed_at=rules.business_time, result={"rule_id": reference.policy_id})
    # Policy text is a structural fixture, not the result of running retrieval.
    hit = PolicyHit(reference=reference, title="退款条款", excerpt="核实损坏后退实付金额", score=1.25)
    return {
        "arguments": {
            "get_order": {"order_id": order.order_id},
            "search_policy": {"query": "损坏退款", "category": "general_goods", "version": reference.version, "limit": 3},
            "read_policy": {"policy_id": reference.policy_id, "version": reference.version, "section": reference.section},
            "request_refund": refund.model_dump(mode="json"), "issue_coupon": coupon.model_dump(mode="json"),
            "get_operation": {"operation_key": operation.operation_key}, "create_handoff": handoff.model_dump(mode="json"),
        },
        "data": {
            "get_order": order.model_dump(mode="json"),
            "search_policy": PolicySearchResult(query="损坏退款", hits=(hit,)).model_dump(mode="json"),
            "read_policy": PolicyDocument(reference=reference, title="退款条款", category="general_goods",
                                          text="核实损坏且在退款窗口内，可退全部实付。").model_dump(mode="json"),
            "request_refund": operation.model_dump(mode="json"),
            "issue_coupon": operation.model_dump(mode="json") | {"operation_key": "coupon-key", "action": coupon.action,
                                                                  "order_id": coupon.order_id, "amount_minor": coupon.amount_minor},
            "get_operation": OperationLookup(operation_key=operation.operation_key, operation=operation).model_dump(mode="json"),
            "create_handoff": operation.model_dump(mode="json") | {"operation_key": "handoff-key", "action": handoff.action,
                                                                    "order_id": handoff.order_id, "amount_minor": None},
        },
    }


@pytest.mark.parametrize("name", list(ToolName))
def test_each_tool_parses_and_round_trips_arguments(samples: dict, name: ToolName) -> None:
    contract = TOOL_CONTRACTS[name]
    record = contract.parse_arguments(samples["arguments"][name])
    assert contract.parse_arguments(json.loads(record.model_dump_json())) == record
    assert "owner_id" not in record.model_dump()


@pytest.mark.parametrize("name", list(ToolName))
def test_each_tool_accepts_only_its_typed_success_payload(samples: dict, name: ToolName) -> None:
    contract = TOOL_CONTRACTS[name]
    record = contract.parse_result({"call_id": "call-1", "status": "ok", "data": samples["data"][name]}, call_id="call-1")
    assert contract.parse_result(json.loads(record.model_dump_json()), call_id="call-1") == record
    assert record.data.model_dump(mode="json") == samples["data"][name]


@pytest.mark.parametrize("name", list(ToolName))
def test_each_tool_has_structured_error_and_call_pairing(name: ToolName) -> None:
    failure = ToolFailure(call_id="call-1", status="error", error=ToolError(code="confirmation_required", message="需先确认提案",
                                                         outcome="not_committed"))
    contract = TOOL_CONTRACTS[name]
    assert contract.parse_result(failure.model_dump(mode="json"), call_id="call-1") == failure
    with pytest.raises(ValueError, match="call_id"):
        contract.parse_result(failure.model_dump(mode="json"), call_id="other-call")


@pytest.mark.parametrize("name", list(ToolName))
def test_model_arguments_cannot_supply_authority(samples: dict, name: ToolName) -> None:
    with pytest.raises(ValidationError):
        TOOL_CONTRACTS[name].parse_arguments(samples["arguments"][name] | {
            "owner_id": "model-owner", "context": {}, "approved": True,
        })


def test_write_arguments_reuse_business_models_and_exclude_runtime_binding(samples: dict) -> None:
    for name, model in (("request_refund", RefundAction), ("issue_coupon", CouponAction), ("create_handoff", HandoffAction)):
        contract = TOOL_CONTRACTS[name]
        assert contract.argument_model is model
        assert contract.requires_approval
        assert "operation_key" not in contract.definition()["inputSchema"]["properties"]
        with pytest.raises(ValidationError):
            contract.parse_arguments(samples["arguments"][name] | {"operation_key": "model-key", "approval_request_id": "model-approval"})
    assert all(TOOL_CONTRACTS[name].read_only for name in ("get_order", "search_policy", "read_policy", "get_operation"))


@pytest.mark.parametrize("amount", [129.0, True])
def test_tool_boundary_rejects_noninteger_money(samples: dict, amount: object) -> None:
    with pytest.raises(ValidationError):
        TOOL_CONTRACTS["request_refund"].parse_arguments(samples["arguments"]["request_refund"] | {"amount_minor": amount})


@pytest.mark.parametrize("limit", [0, 11, True])
def test_search_limit_is_small_positive_integer(limit: object) -> None:
    with pytest.raises(ValidationError):
        TOOL_CONTRACTS["search_policy"].parse_arguments({"query": "refund", "limit": limit})


def test_policy_results_keep_version_time_position_and_empty_search(samples: dict) -> None:
    document = TOOL_CONTRACTS["read_policy"].parse_result({"call_id": "read", "status": "ok", "data": samples["data"]["read_policy"]}, call_id="read")
    assert document.data.reference.version == "mock-policy-v1"
    assert document.data.reference.section == "spec.json#/rules/refund"
    assert document.data.reference.effective_from.tzinfo is not None
    search = TOOL_CONTRACTS["search_policy"].parse_result({"call_id": "search", "status": "ok", "data": {"query": "no match", "hits": []}}, call_id="search")
    assert search.data.hits == ()
    with pytest.raises(ValidationError):
        PolicyHit.model_validate(samples["data"]["search_policy"]["hits"][0] | {"score": float("nan")})


@pytest.mark.parametrize("status", ["pending", "failed"])
def test_write_success_cannot_report_uncommitted_ledger(samples: dict, status: str) -> None:
    data = samples["data"]["request_refund"] | {"status": status, "committed_at": None}
    with pytest.raises(ValidationError):
        TOOL_CONTRACTS["request_refund"].parse_result({"call_id": "write", "status": "ok", "data": data}, call_id="write")


def test_write_result_must_match_tool_and_have_correct_money_shape(samples: dict) -> None:
    with pytest.raises(ValidationError):
        TOOL_CONTRACTS["request_refund"].parse_result({"call_id": "write", "status": "ok", "data": samples["data"]["issue_coupon"]}, call_id="write")
    with pytest.raises(ValidationError):
        TOOL_CONTRACTS["create_handoff"].parse_result({"call_id": "write", "status": "ok", "data": samples["data"]["create_handoff"] | {"amount_minor": 500}}, call_id="write")


@pytest.mark.parametrize("status", ["pending", "succeeded", "failed", None])
def test_lookup_preserves_real_status_or_missing_record(samples: dict, status: str | None) -> None:
    operation = samples["data"]["request_refund"] | {"status": status}
    if status != "succeeded":
        operation["committed_at"] = None
    payload = {"operation_key": "refund-key", "operation": operation if status else None}
    record = TOOL_CONTRACTS["get_operation"].parse_result({"call_id": "lookup", "status": "ok", "data": payload}, call_id="lookup")
    assert record.data.operation.status == status if status else record.data.operation is None
    if status:
        with pytest.raises(ValidationError):
            OperationLookup.model_validate(payload | {"operation_key": "different-key"})


def test_unknown_outcome_requires_original_key_and_explicit_outcome() -> None:
    failure = ToolFailure(call_id="write", status="error", error=UnknownExecutionError(code="response_lost", message="请按原键查询",
                                                                       outcome="unknown", operation_key="original-key"))
    parsed = TOOL_CONTRACTS["request_refund"].parse_result(failure.model_dump(mode="json"), call_id="write")
    assert parsed.error.outcome == "unknown"
    assert parsed.error.operation_key == "original-key"
    with pytest.raises(ValidationError):
        ToolFailure.model_validate({"call_id": "write", "status": "error", "error": {"code": "timeout", "message": "uncertain", "outcome": "unknown"}})
    with pytest.raises(ValidationError):
        ToolError(code="timeout", message="outcome not specified")


def test_results_cannot_mix_success_error_or_omit_call_id(samples: dict) -> None:
    contract = TOOL_CONTRACTS["get_order"]
    data = samples["data"]["get_order"]
    with pytest.raises(ValidationError):
        contract.parse_result({"status": "ok", "data": data}, call_id="call")
    with pytest.raises(ValidationError):
        contract.parse_result({"call_id": "call", "status": "ok", "data": data, "error": {"code": "conflict"}}, call_id="call")


def test_definitions_are_complete_object_schemas_with_no_authority_fields() -> None:
    definitions = tool_definitions()
    assert [tool["name"] for tool in definitions] == [name.value for name in ToolName]
    for tool in definitions:
        assert tool["inputSchema"]["type"] == tool["outputSchema"]["type"] == "object"
        assert tool["inputSchema"]["additionalProperties"] is False
        assert "call_id" not in tool["inputSchema"]["properties"]
        assert "owner_id" not in tool["inputSchema"]["properties"]
        for branch in tool["outputSchema"]["oneOf"]:
            definition = tool["outputSchema"]["$defs"][branch["$ref"].removeprefix("#/$defs/")]
            assert {"call_id", "status"} <= set(definition["required"])
        assert set(tool["outputSchema"]["$defs"]["UnknownExecutionError"]["required"]) == {"code", "message", "outcome", "operation_key"}


def test_schema_cli_is_readonly_and_utf8_from_other_directory(tmp_path) -> None:
    def hashes():
        return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (PROJECT_ROOT / "artifacts/runtime").rglob("*") if path.is_file()}

    before = hashes()
    result = subprocess.run([sys.executable, "-m", "tool_agent_lab.tools.contracts"], cwd=tmp_path,
                            capture_output=True, text=True, encoding="utf-8", check=True)
    data = json.loads(result.stdout)
    assert data["tools"] == tool_definitions()
    assert "查询当前用户" in data["tools"][0]["description"]
    assert hashes() == before
