"""Read exact policy versions for the workbench's citation viewer."""

from fastapi import APIRouter, HTTPException, Request

from tool_agent_lab.tools.contracts import PolicyDocument, ReadPolicyArgs

router = APIRouter(prefix="/policies", tags=["policies"])


@router.get("/{policy_id}", response_model=PolicyDocument)
def read_policy(policy_id: str, version: str, request: Request, section: str = "/") -> PolicyDocument:
    try:
        return request.app.state.policies.read_policy(ReadPolicyArgs(
            policy_id=policy_id, version=version, section=section,
        ))
    except KeyError as error:
        raise HTTPException(404, detail={"code": "policy_not_found"}) from error
