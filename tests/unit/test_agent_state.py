import json

import pytest

from tool_agent_lab.agent.state import identity_of, load_agent_state
from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.runtime.task_service import TaskError, TaskService
from tool_agent_lab.schemas.tasks import TaskCreate
from tool_agent_lab.settings import PROJECT_ROOT
from tool_agent_lab.storage.database import initialize_database


def test_state_uses_owned_persisted_identity_and_roundtrips_json(tmp_path):
    database = tmp_path / "app.sqlite3"
    initialize_database(database)
    rules = BusinessRules.from_file(PROJECT_ROOT / "data/business/v1/spec.json")
    service = TaskService(database, business_time=rules.business_time, model_version="mock-v1")
    task = service.create(TaskCreate(user_message="Please check my order"), owner_id="owner")
    state = load_agent_state(database, task.task_id, owner_id="owner", model_version="mock-v1")
    assert identity_of(state).attempt_id == task.current_attempt_id
    assert identity_of(state).thread_id.startswith("thread-")
    assert state == json.loads(json.dumps(state))
    assert state["messages"] == [{"role": "user", "content": "Please check my order"}]
    assert service.get(task.task_id, owner_id="owner").status == "queued"
    with pytest.raises(TaskError, match="task_not_found"):
        load_agent_state(database, task.task_id, owner_id="someone-else", model_version="mock-v1")
    with pytest.raises(TaskError, match="model_version_mismatch"):
        load_agent_state(database, task.task_id, owner_id="owner", model_version="wrong")
