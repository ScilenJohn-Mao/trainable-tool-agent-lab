"""Transport-independent contracts for the seven after-sales tools."""

import json
import sys
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Generic, Literal, Self, TypeVar

from pydantic import AwareDatetime, Field, TypeAdapter, model_validator

from tool_agent_lab.schemas.actions import CouponAction, HandoffAction, PolicyReference, RefundAction
from tool_agent_lab.schemas.business import Operation, Order
from tool_agent_lab.schemas.common import ContractModel, NonEmptyStr, PositiveInt


class ToolName(StrEnum):
    GET_ORDER = "get_order"
    SEARCH_POLICY = "search_policy"
    READ_POLICY = "read_policy"
    REQUEST_REFUND = "request_refund"
    ISSUE_COUPON = "issue_coupon"
    GET_OPERATION = "get_operation"
    CREATE_HANDOFF = "create_handoff"


class GetOrderArgs(ContractModel):
    order_id: NonEmptyStr


class SearchPolicyArgs(ContractModel):
    query: NonEmptyStr
    category: NonEmptyStr | None = None
    version: NonEmptyStr | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=10)] = 5


class ReadPolicyArgs(ContractModel):
    policy_id: NonEmptyStr
    version: NonEmptyStr
    section: NonEmptyStr | None = None


class GetOperationArgs(ContractModel):
    operation_key: NonEmptyStr


# The action discriminator defaults to the selected tool's name.
RequestRefundArgs = RefundAction
IssueCouponArgs = CouponAction
CreateHandoffArgs = HandoffAction


class PolicyHit(ContractModel):
    reference: PolicyReference
    title: NonEmptyStr
    excerpt: NonEmptyStr
    score: float = Field(allow_inf_nan=False)


class PolicySearchResult(ContractModel):
    query: NonEmptyStr
    hits: tuple[PolicyHit, ...]


class PolicyDocument(ContractModel):
    reference: PolicyReference
    title: NonEmptyStr
    category: NonEmptyStr
    text: NonEmptyStr


class OperationLookup(ContractModel):
    operation_key: NonEmptyStr
    operation: Operation | None

    @model_validator(mode="after")
    def check_key(self) -> Self:
        if self.operation is not None and self.operation.operation_key != self.operation_key:
            raise ValueError("lookup operation does not match the requested key")
        return self


class CommittedOperation(Operation):
    status: Literal["succeeded"] = "succeeded"
    committed_at: AwareDatetime = Field(...)


class CommittedMoneyOperation(CommittedOperation):
    order_id: NonEmptyStr = Field(...)
    amount_minor: PositiveInt = Field(...)


class RefundOperation(CommittedMoneyOperation):
    action: Literal["request_refund"] = "request_refund"


class CouponOperation(CommittedMoneyOperation):
    action: Literal["issue_coupon"] = "issue_coupon"


class HandoffOperation(CommittedOperation):
    action: Literal["create_handoff"] = "create_handoff"
    amount_minor: Literal[None] = None


class ToolError(ContractModel):
    code: NonEmptyStr
    message: NonEmptyStr
    outcome: Literal["not_committed"]
    operation_key: NonEmptyStr | None = None


class UnknownExecutionError(ToolError):
    outcome: Literal["unknown"]
    operation_key: NonEmptyStr = Field(...)


Data = TypeVar("Data", bound=ContractModel)


class ToolSuccess(ContractModel, Generic[Data]):
    call_id: NonEmptyStr
    status: Literal["ok"]
    data: Data


class ToolFailure(ContractModel):
    call_id: NonEmptyStr
    status: Literal["error"]
    error: Annotated[ToolError | UnknownExecutionError, Field(discriminator="outcome")]


@dataclass(frozen=True)
class ToolContract:
    name: ToolName
    description: str
    argument_model: type[ContractModel]
    data_model: type[ContractModel]
    read_only: bool

    @property
    def requires_approval(self) -> bool:
        return not self.read_only

    def parse_arguments(self, values: dict) -> ContractModel:
        return self.argument_model.model_validate(values)

    def _result_adapter(self) -> TypeAdapter:
        return TypeAdapter(Annotated[ToolSuccess[self.data_model] | ToolFailure, Field(discriminator="status")])

    def parse_result(self, values: dict, *, call_id: str) -> ContractModel:
        result = self._result_adapter().validate_python(values)
        if result.call_id != call_id:
            raise ValueError("tool result call_id does not match the call")
        return result

    def definition(self) -> dict:
        output = self._result_adapter().json_schema()
        output["type"] = "object"
        return {
            "name": self.name.value, "description": self.description,
            "inputSchema": self.argument_model.model_json_schema(), "outputSchema": output,
            "annotations": {"readOnlyHint": self.read_only, "openWorldHint": False},
        }


TOOL_CONTRACTS = MappingProxyType({item.name: item for item in (
    ToolContract(ToolName.GET_ORDER, "查询当前用户可访问订单的事实和售后金额。", GetOrderArgs, Order, True),
    ToolContract(ToolName.SEARCH_POLICY, "检索政策片段，保留版本、生效时间和引用位置。", SearchPolicyArgs, PolicySearchResult, True),
    ToolContract(ToolName.READ_POLICY, "读取指定版本政策的正文或条款位置。", ReadPolicyArgs, PolicyDocument, True),
    ToolContract(ToolName.REQUEST_REFUND, "执行已确认的模拟退款，返回实际成功账本；模型参数不授予权限。", RequestRefundArgs, RefundOperation, False),
    ToolContract(ToolName.ISSUE_COUPON, "执行已确认的模拟补偿，返回实际成功账本。", IssueCouponArgs, CouponOperation, False),
    ToolContract(ToolName.GET_OPERATION, "按原操作键核对实际状态；查无记录不等于退款失败。", GetOperationArgs, OperationLookup, True),
    ToolContract(ToolName.CREATE_HANDOFF, "执行已确认的转人工，保存可核对的原因和摘要。", CreateHandoffArgs, HandoffOperation, False),
)})


def tool_definitions() -> list[dict]:
    return [contract.definition() for contract in TOOL_CONTRACTS.values()]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps({"tools": tool_definitions()}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
