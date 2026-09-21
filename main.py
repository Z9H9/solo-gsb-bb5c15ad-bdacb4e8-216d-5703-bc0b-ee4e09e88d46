"""工业二进制报文解析 API（FastAPI）。

启动：
    uvicorn main:app --host 0.0.0.0 --port 8080
配置（环境变量）：
    DB_PATH   SQLite 数据库文件路径，默认 /data/parser.db
    PORT      仅容器/入口脚本使用，应用默认监听 8080
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from db import Database
from parser import DefinitionError, ParseError, parse_message, validate_definition, version_key
from schemas import DefinitionCreate, DefinitionOut, ParseRequest

DB_PATH = os.getenv("DB_PATH", "/data/parser.db")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # SQLite 自动初始化（建表幂等）
    app.state.db = Database(DB_PATH)
    yield


app = FastAPI(
    title="工业二进制报文解析 API",
    version="1.0.0",
    description="注册带版本的二进制报文定义，按位解析十六进制报文并校验校验和。",
    lifespan=lifespan,
)


def _db() -> Database:
    return app.state.db


def _validate_version(version: str) -> None:
    try:
        version_key(version)
    except DefinitionError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------

@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/definitions", response_model=DefinitionOut,
          status_code=201, tags=["definitions"])
def register_definition(payload: DefinitionCreate) -> DefinitionOut:
    """注册（创建）一个带版本的报文定义。同设备类型+版本重复注册返回 409。"""
    _validate_version(payload.version)
    try:
        validate_definition(payload.definition)
        row = _db().create_definition(
            payload.device_type, payload.version,
            payload.definition, payload.description)
    except DefinitionError as e:
        # 结构校验失败 -> 422；唯一约束冲突 -> 409
        if "已存在" in str(e):
            raise HTTPException(status_code=409, detail=str(e)) from e
        raise HTTPException(status_code=422, detail=f"报文定义不合法: {e}") from e
    return DefinitionOut(**row)


@app.get("/api/v1/definitions", response_model=list[DefinitionOut],
         tags=["definitions"])
def list_definitions(
    device_type: str | None = Query(None, description="按设备类型过滤"),
    active: bool | None = Query(None, description="按启用状态过滤"),
) -> list[DefinitionOut]:
    """列出全部定义，可按设备类型、启用状态过滤。"""
    rows = _db().list_definitions(device_type, active)
    return [DefinitionOut(**r) for r in rows]


@app.get("/api/v1/definitions/{device_type}/latest",
         response_model=DefinitionOut, tags=["definitions"])
def get_latest_definition(
    device_type: str,
    include_inactive: bool = Query(False, description="是否包含已停用版本"),
) -> DefinitionOut:
    """按设备类型查询语义版本号最新的定义（默认仅启用版本）。"""
    row = _db().get_latest_version(device_type, active_only=not include_inactive)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"设备类型 {device_type!r} 不存在可用的报文定义")
    return DefinitionOut(**row)


@app.get("/api/v1/definitions/{device_type}/{version}",
         response_model=DefinitionOut, tags=["definitions"])
def get_definition(device_type: str, version: str) -> DefinitionOut:
    """获取指定设备类型+版本的定义。"""
    row = _db().get_definition(device_type, version)
    if row is None:
        raise HTTPException(status_code=404, detail="报文定义不存在")
    return DefinitionOut(**row)


@app.post("/api/v1/definitions/{device_type}/{version}/deactivate",
         tags=["definitions"])
def deactivate_definition(device_type: str, version: str) -> dict[str, Any]:
    """停用指定定义（幂等：重复停用不报错）。"""
    row = _db().set_active(device_type, version, False)
    if row is None:
        raise HTTPException(status_code=404, detail="报文定义不存在")
    return {"device_type": device_type, "version": version, "active": False}


@app.post("/api/v1/parse", tags=["parse"])
def parse(payload: ParseRequest) -> JSONResponse:
    """提交指定版本的十六进制报文，返回字段值、位偏移、分支路径与校验结果。

    HTTP 200 表示报文被完整解析；响应体 valid=false 表示校验和/尾字节等
    内容校验失败（详情见 errors）。报文结构无法解析时返回 422。
    """
    row = _db().get_definition(payload.device_type, payload.version)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"报文定义不存在: {payload.device_type}@{payload.version}")
    if not row["active"]:
        raise HTTPException(
            status_code=409,
            detail=f"报文定义 {payload.device_type}@{payload.version} 已停用")
    try:
        result = parse_message(row["definition"], payload.hex_payload)
    except ParseError as e:
        raise HTTPException(status_code=422, detail=f"报文解析失败: {e}") from e
    except DefinitionError as e:
        raise HTTPException(status_code=422, detail=f"报文定义不合法: {e}") from e

    body = {
        "device_type": payload.device_type,
        "version": payload.version,
        "definition_fingerprint": row["fingerprint"],
        "parse": result.to_dict(),
    }
    return JSONResponse(status_code=200, content=body)
