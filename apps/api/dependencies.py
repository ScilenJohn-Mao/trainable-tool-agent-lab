"""Resolve local application identity and shared services from app state."""

from typing import Annotated

from fastapi import Depends, Request

from tool_agent_lab.runtime.approvals import ApprovalService
from tool_agent_lab.runtime.task_service import TaskService


def get_owner_id(request: Request) -> str:
    """Local developer identity is configured by the server, never by HTTP input."""
    return request.app.state.settings.dev_owner_id


def get_task_service(request: Request) -> TaskService:
    return request.app.state.tasks


def get_approval_service(request: Request) -> ApprovalService:
    return request.app.state.approvals


Owner = Annotated[str, Depends(get_owner_id)]
Tasks = Annotated[TaskService, Depends(get_task_service)]
Approvals = Annotated[ApprovalService, Depends(get_approval_service)]
