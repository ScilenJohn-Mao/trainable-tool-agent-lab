"""Scenario expectations and user behaviour, separate from model-visible input."""

from typing import Literal

from pydantic import AwareDatetime

from tool_agent_lab.agent.config import AgentConfig
from tool_agent_lab.agent.model_client import ModelConfig
from tool_agent_lab.schemas.actions import ApprovalDecision
from tool_agent_lab.schemas.business import NonNegativeInt
from tool_agent_lab.schemas.common import ContractModel, NonEmptyStr
from tool_agent_lab.schemas.tasks import TaskCreate

TaskType = Literal["policy_refund", "refund_and_coupon", "reconcile_operation", "clarify_or_handoff"]
WriteAction = Literal["request_refund", "issue_coupon", "create_handoff"]


class UserBehaviour(ContractModel):
    input_messages: tuple[NonEmptyStr, ...] = ()
    approval_decisions: tuple[ApprovalDecision, ...] = ()


class ScenarioExpectation(ContractModel):
    order_id: NonEmptyStr | None
    refund_total_minor: NonNegativeInt
    coupon_total_minor: NonNegativeInt
    new_actions: tuple[WriteAction, ...] = ()
    must_clarify: bool = False
    must_query_operation: bool = False
    handoff_reason: NonEmptyStr | None = None
    rule_ids: tuple[NonEmptyStr, ...] = ()
    outcome: Literal["resolved", "answered", "rejected"]


class Scenario(ContractModel):
    scenario_id: NonEmptyStr
    family_id: NonEmptyStr
    task_type: TaskType
    fixture_version: NonEmptyStr
    business_time: AwareDatetime
    owner_id: NonEmptyStr
    request: TaskCreate
    known_operation_key: NonEmptyStr | None = None
    user: UserBehaviour
    expected: ScenarioExpectation


class ExperimentConfig(ContractModel):
    model: ModelConfig
    agent: AgentConfig
