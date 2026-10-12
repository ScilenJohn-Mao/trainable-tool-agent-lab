"""Submit scenario replies to existing input and proposal confirmation endpoints."""

from uuid import uuid4

from tool_agent_lab.agent.graph import Agent
from tool_agent_lab.agent.inputs import InputRequest
from tool_agent_lab.schemas.actions import ApprovalRequest
from tool_agent_lab.schemas.scenarios import UserBehaviour
from tool_agent_lab.schemas.tasks import Task


class UserSimulator:
    def __init__(self, behaviour: UserBehaviour) -> None:
        self.behaviour = behaviour
        self.input_index = 0
        self.approval_index = 0
        self.requests: dict[tuple, InputRequest | ApprovalRequest] = {}

    def request(self, waiting: dict) -> InputRequest | ApprovalRequest | None:
        """Repeated polling of one waiting point returns the same request identity."""
        if waiting["kind"] == "input":
            key = ("input", waiting["request_id"])
            if key not in self.requests:
                if self.input_index == len(self.behaviour.input_messages):
                    return None
                self.requests[key] = InputRequest(
                    request_id=f"sim-input-{uuid4().hex}", input_request_id=waiting["request_id"],
                    message=self.behaviour.input_messages[self.input_index],
                )
                self.input_index += 1
        elif waiting["kind"] == "approval":
            proposal = waiting["proposal"]
            key = ("approval", proposal["proposal_id"], proposal["proposal_version"])
            if key not in self.requests:
                if self.approval_index == len(self.behaviour.approval_decisions):
                    return None
                self.requests[key] = ApprovalRequest(
                    request_id=f"sim-approval-{uuid4().hex}", proposal_id=proposal["proposal_id"],
                    proposal_version=proposal["proposal_version"],
                    decision=self.behaviour.approval_decisions[self.approval_index],
                )
                self.approval_index += 1
        else:
            raise ValueError(f"Unsupported user waiting kind: {waiting['kind']}")
        return self.requests[key]

    async def respond(self, agent: Agent, task: Task, waiting: dict) -> dict | None:
        request = self.request(waiting)
        if isinstance(request, InputRequest):
            return await agent.submit_input(task.task_id, request, owner_id=task.owner_id)
        if isinstance(request, ApprovalRequest):
            return await agent.submit_approval(task.task_id, request, owner_id=task.owner_id)
        return None
