"""Serve local HTTP tasks and human decisions using the shared runtime services."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from apps.api.routes.tasks import router
from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.runtime.approvals import ApprovalService
from tool_agent_lab.runtime.task_service import TaskError, TaskService
from tool_agent_lab.settings import Settings, load_settings
from tool_agent_lab.storage.database import initialize_database


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        rules = BusinessRules.from_file(settings.business_data_dir / "spec.json")
        initialize_database(settings.app_db_path)
        application.state.tasks = TaskService(
            settings.app_db_path, business_time=rules.business_time,
            model_version=settings.mode, config_version=settings.config_version,
        )
        application.state.approvals = ApprovalService(settings.app_db_path, business_time=rules.business_time)
        yield

    application = FastAPI(title="Trainable Tool Agent Lab", version="0.1.0", lifespan=lifespan)
    application.state.settings = settings
    application.include_router(router)

    @application.exception_handler(TaskError)
    async def task_error_handler(request: Request, error: TaskError) -> JSONResponse:
        status_code = 404 if error.code in {"task_not_found", "order_not_found"} else 409
        return JSONResponse(status_code=status_code, content={"detail": {"code": error.code}})

    return application


app = create_app()
