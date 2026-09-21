"""Industrial binary message parsing API."""

import re
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query

from . import database
from .parser import ParseError, parse_message
from .schemas import DefinitionCreate, ParseRequest, validate_definition


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    yield


app = FastAPI(
    title="Industrial Binary Message Parser",
    description=(
        "Register versioned binary message definitions (bit widths, endianness, "
        "conditional branches, checksums) and parse hex payloads against them."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# Make the service usable even if the lifespan hook is bypassed (e.g. tests).
database.init_db()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/v1/definitions", status_code=201)
def register_definition(defn: DefinitionCreate):
    """Register a new versioned message definition."""
    try:
        validate_definition(defn)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    record = database.create_definition(
        device_type=defn.device_type,
        version=defn.version,
        description=defn.description,
        fields=[f.model_dump(exclude_none=True) for f in defn.fields],
        checksum=defn.checksum.model_dump() if defn.checksum else None,
    )
    if record is None:
        raise HTTPException(
            status_code=409,
            detail=f"definition '{defn.device_type}' version '{defn.version}' already exists",
        )
    return record


@app.get("/api/v1/definitions")
def list_definitions(
    device_type: Optional[str] = Query(default=None),
    include_inactive: bool = Query(default=False),
):
    """List registered definitions (active ones by default)."""
    return {
        "count": len(
            items := database.list_definitions(device_type, include_inactive)
        ),
        "items": items,
    }


@app.get("/api/v1/definitions/{device_type}/latest")
def get_latest_definition(device_type: str):
    """Get the latest active version for a device type."""
    record = database.get_latest(device_type, active_only=True)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"no active definition found for device type '{device_type}'",
        )
    return record


@app.get("/api/v1/definitions/{device_type}/{version}")
def get_definition(device_type: str, version: str):
    record = database.get_definition(device_type, version)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"definition '{device_type}' version '{version}' not found",
        )
    return record


@app.post("/api/v1/definitions/{device_type}/{version}/deactivate")
def deactivate_definition(device_type: str, version: str):
    """Deactivate a definition. It stays parseable by explicit version but is
    excluded from listing (by default) and from latest-version lookups."""
    record = database.deactivate(device_type, version)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"definition '{device_type}' version '{version}' not found",
        )
    return record


_HEX_CLEANER = re.compile(r"(0[xX])|[\s_-]")


@app.post("/api/v1/parse")
def parse(req: ParseRequest):
    """Parse a hex payload against a definition (latest active version unless
    an explicit version is given)."""
    if req.version:
        record = database.get_definition(req.device_type, req.version)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail=f"definition '{req.device_type}' version '{req.version}' not found",
            )
    else:
        record = database.get_latest(req.device_type, active_only=True)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail=f"no active definition found for device type '{req.device_type}'",
            )

    cleaned = _HEX_CLEANER.sub("", req.hex)
    if not cleaned:
        raise HTTPException(status_code=422, detail="hex payload is empty")
    try:
        data = bytes.fromhex(cleaned)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="invalid hex payload: non-hex characters or odd number of digits",
        )

    try:
        result = parse_message(record["fields"], record["checksum"], data)
    except ParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "device_type": record["device_type"],
        "version": record["version"],
        "definition_active": record["active"],
        **result,
    }
