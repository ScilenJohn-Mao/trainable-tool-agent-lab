"""Serve local HTTP tasks and human decisions using the shared runtime services."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from apps.api.routes.tasks import router
from apps.api.routes.events import router as events_router
from apps.api.routes.policies import router as policies_router
from tool_agent_lab.agent.config import load_agent_config
from tool_agent_lab.agent.inputs import InputService
from tool_agent_lab.agent.model_client import load_model_config
from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.knowledge.ingest import PolicyCatalog
from tool_agent_lab.runtime.approvals import ApprovalService
from tool_agent_lab.runtime.task_service import TaskError, TaskService
from tool_agent_lab.settings import Settings, load_settings
from tool_agent_lab.storage.database import initialize_database


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        rules = BusinessRules.from_file(settings.business_data_dir / "spec.json")
        application.state.policies = PolicyCatalog.from_file(settings.business_data_dir / "policies.json")
        initialize_database(settings.app_db_path)
        application.state.tasks = TaskService(
            settings.app_db_path, business_time=rules.business_time,
            model_version=load_model_config(settings.model_config_file).version,
            config_version=load_agent_config(settings.agent_config_file).version,
        )
        application.state.approvals = ApprovalService(settings.app_db_path, business_time=rules.business_time)
        application.state.inputs = InputService(settings.app_db_path, business_time=rules.business_time)
        yield

    application = FastAPI(title="Trainable Tool Agent Lab", version="0.1.0", lifespan=lifespan)
    application.state.settings = settings
    application.include_router(router)
    application.include_router(events_router)
    application.include_router(policies_router)

    @application.exception_handler(TaskError)
    async def task_error_handler(request: Request, error: TaskError) -> JSONResponse:
        status_code = 404 if error.code in {"task_not_found", "order_not_found"} else 409
        return JSONResponse(status_code=status_code, content={"detail": {"code": error.code}})

    return application


app = create_app()
