"""FastAPI 请求/响应模型（Pydantic v2）。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DefinitionCreate(StrictModel):
    device_type: str = Field(..., min_length=1, max_length=128,
                             description="设备类型，如 sensor-ht")
    version: str = Field(..., min_length=1, max_length=32,
                         description="语义版本号，如 1.0.0")
    definition: dict[str, Any] = Field(..., description="报文定义（fields/endian）")
    description: str = Field("", max_length=512, description="可选说明")


class DefinitionOut(BaseModel):
    id: int
    device_type: str
    version: str
    definition: dict[str, Any]
    fingerprint: str
    description: str
    active: bool
    created_at: str


class ParseRequest(StrictModel):
    device_type: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1, description="报文定义版本")
    hex_payload: str = Field(..., description="十六进制报文，空格/0x 前缀可省略")


class DeactivateResponse(BaseModel):
    device_type: str
    version: str
    active: bool
