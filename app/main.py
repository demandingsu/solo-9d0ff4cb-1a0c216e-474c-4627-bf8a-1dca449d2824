"""补考缺考核定 API。

接口：
- POST /admin/sessions          按期预登记学员与可去考场
- POST /rosters/batches         一批多场交册（每册必带名单，显式空册也算已交），返回收据
- GET  /rosters/batches/{id}    按批号取回原回执
- GET  /sessions/{id}/absences  查本期缺考名单
- GET  /sessions/{id}/rooms     查各场签到册交册状态
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import service
from .database import init_db
from .schemas import (
    AbsentListResponse,
    Receipt,
    RegisterRequest,
    RoomStatusResponse,
    SubmitRostersRequest,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="补考缺考核定 API",
    description="按期预登记学员/考场，按批交签到册，自动核定缺考名单。",
    version="1.1.0",
    lifespan=lifespan,
)


@app.exception_handler(service.ApiError)
def _api_error_handler(_: Request, exc: service.ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "message": exc.message},
    )


@app.exception_handler(RequestValidationError)
def _validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": "validation_error", "detail": exc.errors()},
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/admin/sessions", status_code=201)
def register_session(payload: RegisterRequest) -> dict:
    return service.register_session(payload.model_dump())


@app.post("/rosters/batches", response_model=Receipt)
def submit_rosters(payload: SubmitRostersRequest) -> dict:
    """一批一场或多场交册。

    每册必须显式携带 `student_ids`：任一册漏传即整批拒收
    （`missing_student_ids`，批号不占用，补齐后可原批号重交）；
    显式提交 `[]` 表示空册，空册也算已交。
    """
    return service.submit_rosters(payload)


@app.get("/rosters/batches/{batch_id}", response_model=Receipt)
def get_receipt(batch_id: str) -> dict:
    return service.get_receipt(batch_id)


@app.get("/sessions/{session_id}/absences", response_model=AbsentListResponse)
def get_absences(session_id: str) -> dict:
    return service.get_absent_list(session_id)


@app.get("/sessions/{session_id}/rooms", response_model=RoomStatusResponse)
def get_rooms(session_id: str) -> dict:
    return service.get_room_status(session_id)
