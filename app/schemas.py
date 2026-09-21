"""Pydantic request models and definition-spec validation."""

from typing import List, Literal, Optional, Union

from pydantic import BaseModel, Field

from .checksum import ALGORITHMS


class Condition(BaseModel):
    field: str
    op: Literal["eq", "ne", "gt", "ge", "lt", "le", "in"]
    value: Union[int, List[int]]


class FieldDef(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    type: Literal["uint", "int", "bool", "hex", "ascii"] = "uint"
    bit_length: int = Field(gt=0, le=4096)
    endianness: Literal["big", "little"] = "big"
    scale: Optional[float] = None
    offset: Optional[float] = None
    condition: Optional[Condition] = None


class ChecksumDef(BaseModel):
    field: str
    algorithm: Literal["sum8", "xor8", "crc16-ccitt", "crc16-modbus"]


class DefinitionCreate(BaseModel):
    device_type: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    description: str = ""
    fields: List[FieldDef]
    checksum: Optional[ChecksumDef] = None


class ParseRequest(BaseModel):
    device_type: str
    version: Optional[str] = None  # omitted -> latest active version
    hex: str = Field(min_length=2)


def validate_definition(defn: DefinitionCreate) -> None:
    """Cross-field semantic validation. Raises ValueError on problems."""
    if not defn.fields:
        raise ValueError("fields must not be empty")

    seen: set[str] = set()
    numeric_seen: set[str] = set()  # fields usable in conditions
    for f in defn.fields:
        if f.name in seen:
            raise ValueError(f"duplicate field name: '{f.name}'")
        seen.add(f.name)

        if f.type == "bool" and f.bit_length != 1:
            raise ValueError(f"field '{f.name}': bool fields must be 1 bit")
        if f.type in ("hex", "ascii") and f.bit_length % 8 != 0:
            raise ValueError(
                f"field '{f.name}': {f.type} fields require bit_length to be a multiple of 8"
            )
        if f.endianness == "little":
            if f.type not in ("uint", "int"):
                raise ValueError(
                    f"field '{f.name}': little-endian only applies to uint/int fields"
                )
            if f.bit_length % 8 != 0:
                raise ValueError(
                    f"field '{f.name}': little-endian requires bit_length to be a multiple of 8"
                )
        if (f.scale is not None or f.offset is not None) and f.type not in ("uint", "int"):
            raise ValueError(
                f"field '{f.name}': scale/offset only apply to uint/int fields"
            )
        if f.condition:
            c = f.condition
            if c.field not in numeric_seen:
                raise ValueError(
                    f"field '{f.name}': condition references '{c.field}', which is not a "
                    f"previously defined uint/int/bool field"
                )
            if c.op == "in" and not isinstance(c.value, list):
                raise ValueError(
                    f"field '{f.name}': operator 'in' requires a list value"
                )
            if c.op != "in" and not isinstance(c.value, int):
                raise ValueError(
                    f"field '{f.name}': operator '{c.op}' requires an integer value"
                )
        if f.type in ("uint", "int", "bool"):
            numeric_seen.add(f.name)

    if defn.checksum:
        target = next((f for f in defn.fields if f.name == defn.checksum.field), None)
        if target is None:
            raise ValueError(
                f"checksum field '{defn.checksum.field}' is not defined in fields"
            )
        if target.type != "uint":
            raise ValueError(f"checksum field '{target.name}' must be of type uint")
        _, width = ALGORITHMS[defn.checksum.algorithm]
        if target.bit_length != width:
            raise ValueError(
                f"checksum field '{target.name}' must be {width} bits for algorithm "
                f"'{defn.checksum.algorithm}'"
            )
