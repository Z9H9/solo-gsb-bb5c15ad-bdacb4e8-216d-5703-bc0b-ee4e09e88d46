"""工业二进制报文按位解析核心。

功能：
- 按 bit 位偏移解析字段，支持有/无符号整数、布尔、枚举、定长、填充、校验和字段；
- 每字段可覆盖端序（little / big），支持位域（MSB 在前）；
- 支持 switch 条件分支与 when 字段级条件，解析时返回实际分支路径；
- 支持 checksum8/xor、sum8/sum16、crc8/smbus、crc16/modbus、crc16/ccitt-false；
- 对报文定义做结构校验（注册前调用）。

纯标准库实现，无外部依赖。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 常量与异常
# ---------------------------------------------------------------------------

FIELD_TYPES = {
    "uint", "int", "bool", "enum", "fixed", "padding", "checksum", "switch", "group",
}
ENDIANS = ("little", "big")
CHECKSUM_ALGS = (
    "xor8", "checksum8", "sum8", "sum16",
    "crc8", "crc8/smbus", "crc16/modbus", "crc16/ccitt-false",
)
OPS = {"==": lambda a, b: a == b,
       "!=": lambda a, b: a != b,
       ">": lambda a, b: a > b,
       ">=": lambda a, b: a >= b,
       "<": lambda a, b: a < b,
       "<=": lambda a, b: a <= b,
       "in": lambda a, b: a in b,
       "notin": lambda a, b: a not in b}

# 各校验算法默认位宽
_CHECKSUM_BITS = {
    "xor8": 8, "checksum8": 8, "sum8": 8,
    "sum16": 16, "crc8": 8, "crc8/smbus": 8,
    "crc16/modbus": 16, "crc16/ccitt-false": 16,
}

# CRC 参数预设（width, poly, init, refin, refout, xorout）
_CRC_PRESETS = {
    "crc8":         dict(width=8,  poly=0x07, init=0x00, refin=False, refout=False, xorout=0x00),
    "crc8/smbus":   dict(width=8,  poly=0x07, init=0x00, refin=False, refout=False, xorout=0x00),
    "crc16/modbus": dict(width=16, poly=0x8005, init=0xFFFF, refin=True, refout=True, xorout=0x0000),
    "crc16/ccitt-false": dict(width=16, poly=0x1021, init=0x0000, refin=False, refout=False, xorout=0x0000),
}


class DefinitionError(ValueError):
    """报文定义本身不合法（注册期校验失败）。"""


class ParseError(ValueError):
    """报文结构无法继续解析（如长度不足、字段未字节对齐等）。"""


# ---------------------------------------------------------------------------
# 解析结果
# ---------------------------------------------------------------------------

@dataclass
class ParsedField:
    name: str
    type: str
    bit_offset: int
    bits: int
    value: Any = None
    raw_hex: str | None = None
    endian: str | None = None
    enum_label: str | None = None
    note: str | None = None
    taken: bool = True

    def to_dict(self) -> dict[str, Any]:
        d = {
            "name": self.name,
            "type": self.type,
            "bit_offset": self.bit_offset,
            "bits": self.bits,
            "taken": self.taken,
        }
        if self.taken:
            d["value"] = self.value
            d["raw_hex"] = self.raw_hex
            if self.endian is not None:
                d["endian"] = self.endian
            if self.enum_label is not None:
                d["enum_label"] = self.enum_label
        if self.note:
            d["note"] = self.note
        return d


@dataclass
class ChecksumResult:
    field: str
    algorithm: str
    expected: int
    actual: int
    passed: bool
    covered_bytes: str
    bit_offset: int
    bits: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "algorithm": self.algorithm,
            "expected": self.expected,
            "actual": self.actual,
            "expected_hex": f"0x{self.expected:0{self.bits // 4}X}",
            "actual_hex": f"0x{self.actual:0{self.bits // 4}X}",
            "passed": self.passed,
            "covered_byte_range": self.covered_bytes,
            "bit_offset": self.bit_offset,
            "bits": self.bits,
        }


@dataclass
class ParseResult:
    fields: list[ParsedField] = field(default_factory=list)
    checksums: list[ChecksumResult] = field(default_factory=list)
    branch_path: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    total_bits: int = 0
    byte_length: int = 0
    valid: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "byte_length": self.byte_length,
            "total_bits_consumed": self.total_bits,
            "fields": [f.to_dict() for f in self.fields],
            "branch_path": self.branch_path,
            "checksums": [c.to_dict() for c in self.checksums],
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# 报文定义校验（注册期）
# ---------------------------------------------------------------------------

def validate_definition(definition: dict[str, Any]) -> None:
    """校验报文定义结构，非法时抛 DefinitionError。"""
    if not isinstance(definition, dict):
        raise DefinitionError("定义必须是 JSON 对象")
    endian = definition.get("endian", "big")
    if endian not in ENDIANS:
        raise DefinitionError(f"全局 endian 仅支持 {ENDIANS}，得到 {endian!r}")

    fields = definition.get("fields")
    if not isinstance(fields, list) or not fields:
        raise DefinitionError("fields 必须是非空数组")

    _validate_fields(fields, endian, parent="")


def _validate_fields(fields: list[dict], default_endian: str, parent: str) -> None:
    for f in fields:
        if not isinstance(f, dict):
            raise DefinitionError(f"{parent or '<root>'}: 字段必须是对象")
        name = f.get("name")
        where = f"{parent}.{name}" if parent else str(name or "<unnamed>")
        ftype = f.get("type")
        if not name or not isinstance(name, str):
            raise DefinitionError(f"{parent or '<root>'}: 缺少 name")
        if ftype not in FIELD_TYPES:
            raise DefinitionError(f"{where}: type 必须是 {sorted(FIELD_TYPES)} 之一")

        if ftype in ("uint", "int", "bool", "enum", "fixed", "padding", "checksum"):
            bits = f.get("bits")
            if not isinstance(bits, int) or isinstance(bits, bool) or bits <= 0:
                raise DefinitionError(f"{where}: bits 必须是正整数")
            if ftype == "bool" and bits != 1:
                raise DefinitionError(f"{where}: bool 字段 bits 必须为 1")
            if f.get("endian", default_endian) not in ENDIANS:
                raise DefinitionError(f"{where}: 非法 endian")
            if ftype == "enum":
                mapping = f.get("values")
                if not isinstance(mapping, dict) or not mapping:
                    raise DefinitionError(f"{where}: enum 需要非空 values 映射")
                for k in mapping:
                    _enum_key_to_int(k, where)
            if ftype == "fixed":
                if "value" not in f:
                    raise DefinitionError(f"{where}: fixed 字段需要 value")
                if not isinstance(f["value"], int) or isinstance(f["value"], bool):
                    raise DefinitionError(f"{where}: fixed value 必须是整数")
                if not 0 <= f["value"] < (1 << bits):
                    raise DefinitionError(f"{where}: fixed value 超出 bits 表示范围")
            if ftype == "checksum":
                alg = f.get("algorithm")
                if alg not in CHECKSUM_ALGS:
                    raise DefinitionError(
                        f"{where}: algorithm 必须是 {CHECKSUM_ALGS} 之一")
                if bits % 8 != 0:
                    raise DefinitionError(f"{where}: checksum 字段必须字节对齐(bits 为 8 的倍数)")
                if _CHECKSUM_BITS[alg] != bits:
                    raise DefinitionError(
                        f"{where}: {alg} 要求 bits={_CHECKSUM_BITS[alg]}，得到 {bits}")
                cover = f.get("cover", "before")
                if cover not in ("before", "all_except"):
                    if not (isinstance(cover, dict) and "start_bit" in cover
                            and "end_bit" in cover):
                        raise DefinitionError(
                            f"{where}: cover 必须是 before / all_except / {{start_bit,end_bit}}")
                _validate_crc_params(where, f)
        elif ftype == "switch":
            if "selector" not in f:
                raise DefinitionError(f"{where}: switch 需要 selector")
            cases = f.get("cases")
            if not isinstance(cases, list) or not cases:
                raise DefinitionError(f"{where}: switch cases 必须是非空数组")
            for i, c in enumerate(cases):
                if not isinstance(c, dict) or "fields" not in c:
                    raise DefinitionError(f"{where}.cases[{i}]: 需要 fields 数组")
                cfields = c["fields"]
                if not isinstance(cfields, list) or not cfields:
                    raise DefinitionError(f"{where}.cases[{i}]: fields 必须是非空数组")
                _validate_fields(cfields, default_endian, f"{where}.cases[{i}]")
            if "when" in f:
                _validate_when(f["when"], where)
        elif ftype == "group":
            sub = f.get("fields")
            if not isinstance(sub, list) or not sub:
                raise DefinitionError(f"{where}: group 需要非空 fields 数组")
            _validate_fields(sub, default_endian, where)

        if "when" in f and ftype != "switch":
            _validate_when(f["when"], where)


def _validate_when(when: Any, where: str) -> None:
    if not isinstance(when, dict) or "field" not in when or "op" not in when:
        raise DefinitionError(f"{where}: when 必须包含 field 与 op")
    if when["op"] not in OPS:
        raise DefinitionError(f"{where}: when.op 必须是 {sorted(OPS)}")


def _validate_crc_params(where: str, f: dict) -> None:
    params = f.get("crc_params")
    if params is None:
        return
    if not isinstance(params, dict):
        raise DefinitionError(f"{where}: crc_params 必须是对象")
    for k in ("poly", "init", "xorout"):
        if k in params and (not isinstance(params[k], int) or isinstance(params[k], bool)):
            raise DefinitionError(f"{where}: crc_params.{k} 必须是整数")
    for k in ("refin", "refout"):
        if k in params and not isinstance(params[k], bool):
            raise DefinitionError(f"{where}: crc_params.{k} 必须是布尔")
    if "width" in params and params["width"] != f["bits"]:
        raise DefinitionError(f"{where}: crc_params.width 必须与 bits 相同")


def _enum_key_to_int(key: Any, where: str) -> int:
    if isinstance(key, bool):
        raise DefinitionError(f"{where}: enum key 不能是布尔")
    if isinstance(key, int):
        return key
    if isinstance(key, str):
        try:
            return int(key, 0)
        except ValueError as e:
            raise DefinitionError(f"{where}: enum key {key!r} 不是整数") from e
    raise DefinitionError(f"{where}: enum key {key!r} 类型非法")


# ---------------------------------------------------------------------------
# 校验和计算
# ---------------------------------------------------------------------------

def _reflect(data: int, width: int) -> int:
    r = 0
    for i in range(width):
        if data & (1 << i):
            r |= 1 << (width - 1 - i)
    return r


def _crc(data: bytes, params: dict) -> int:
    width = params["width"]
    top = 1 << (width - 1)
    mask = (1 << width) - 1
    crc = params["init"]
    for b in data:
        if params["refin"]:
            b = _reflect(b, 8)
        crc ^= b << (width - 8)
        for _ in range(8):
            if crc & top:
                crc = ((crc << 1) ^ params["poly"]) & mask
            else:
                crc = (crc << 1) & mask
    if params["refout"]:
        crc = _reflect(crc, width)
    return (crc ^ params["xorout"]) & mask


def _compute_checksum(data: bytes, algorithm: str, params: dict | None) -> int:
    if algorithm == "xor8":
        v = 0
        for b in data:
            v ^= b
        return v
    if algorithm in ("checksum8", "sum8"):
        return sum(data) & 0xFF
    if algorithm == "sum16":
        return sum(data) & 0xFFFF
    crc_p = dict(_CRC_PRESETS[algorithm])
    if params:
        crc_p.update(params)
    return _crc(data, crc_p)


# ---------------------------------------------------------------------------
# 位读取
# ---------------------------------------------------------------------------

def _read_bits(buf: bytes, bit_pos: int, bits: int, endian: str) -> int:
    """从 buf 的 bit_pos 处读取 bits 位。

    位序约定：bit 0 = 首字节最高位(MSB)。
    - 跨字节的位域按大端（网络序）连续打包；
    - 完全字节对齐的多字节字段按字段 endian 解释。
    """
    nbytes = len(buf)
    if bit_pos < 0 or bit_pos + bits > nbytes * 8:
        raise ParseError(
            f"报文长度不足：需要从 bit {bit_pos} 读取 {bits} 位，"
            f"报文共 {nbytes} 字节")
    if bits % 8 == 0 and bit_pos % 8 == 0:
        start = bit_pos // 8
        raw = buf[start:start + bits // 8]
        return int.from_bytes(raw, endian)
    # 非对齐：按 MSB-first 连续取位
    val = 0
    for i in range(bits):
        p = bit_pos + i
        byte = buf[p // 8]
        bit = (byte >> (7 - (p % 8))) & 1
        val = (val << 1) | bit
    return val


def _twos_complement(v: int, bits: int) -> int:
    if v & (1 << (bits - 1)):
        v -= 1 << bits
    return v


def _hex_value(v: int, bits: int) -> str:
    width = max(1, (bits + 3) // 4)
    return f"0x{v:0{width}X}"


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def parse_message(definition: dict[str, Any], hex_string: str) -> ParseResult:
    """按定义解析十六进制报文。

    结构错误（长度不足、未对齐等）抛 ParseError；
    可完成解析但内容校验失败（校验和、尾字节等）记录在 result.errors，valid=False。
    """
    validate_definition(definition)
    try:
        text = hex_string.replace(" ", "").replace("0x", "").replace("\n", "")
        if not text:
            raise ValueError("空报文")
        data = bytes.fromhex(text)
    except ValueError as e:
        raise ParseError(f"非法的十六进制报文: {e}") from e
    if len(text) % 2 != 0:
        raise ParseError("十六进制字符数必须为偶数")

    result = ParseResult(byte_length=len(data))
    ctx = _ParseCtx(data, definition.get("endian", "big"), result)
    ctx.walk(definition["fields"], prefix="")
    result.total_bits = ctx.cursor

    if ctx.cursor != len(data) * 8:
        result.errors.append(
            f"存在未消费的尾字节：定义消费 {ctx.cursor} 位，报文共 {len(data) * 8} 位")

    result.valid = not result.errors
    return result


class _ParseCtx:
    def __init__(self, data: bytes, default_endian: str, result: ParseResult):
        self.data = data
        self.cursor = 0
        self.default_endian = default_endian
        self.result = result
        self.leaves: list[dict[str, Any]] = []  # 已取值叶子（路径名/值/位偏移）
        self.pending_checksums: list[tuple[str, int, int, dict]] = []

    # -- 引用解析 ---------------------------------------------------------

    def resolve_value(self, ref: str, where: str) -> int:
        leaf = self._find_leaf(ref, where)
        return leaf["value"]

    def _find_leaf(self, ref: str, where: str) -> dict[str, Any]:
        if "." in ref:
            for leaf in reversed(self.leaves):
                if leaf["name"] == ref:
                    return leaf
        else:
            exact = [l for l in self.leaves if l["name"].split(".")[-1] == ref]
            if len(exact) > 1:
                raise ParseError(f"{where}: 条件引用 {ref!r} 存在歧义，请使用完整路径")
            if exact:
                return exact[0]
        raise ParseError(f"{where}: 条件引用的字段 {ref!r} 不存在或尚未解析")

    def _label_for(self, leaf: dict[str, Any]) -> str | None:
        fdef = leaf.get("def", {})
        if fdef.get("type") == "enum":
            values = fdef["values"]
            v = leaf["value"]
            return values.get(str(v), values.get(v))
        return None

    # -- 遍历 -------------------------------------------------------------

    def walk(self, fields: list[dict], prefix: str) -> None:
        for f in fields:
            ftype = f["type"]
            when = f.get("when")
            if when is not None and not self._eval_when(when, self._name_of(f, prefix)):
                self.result.fields.append(ParsedField(
                    name=self._name_of(f, prefix), type=ftype,
                    bit_offset=self.cursor, bits=0, taken=False,
                    note="when 条件不满足，字段跳过"))
                continue
            if ftype == "switch":
                self._do_switch(f, prefix)
            elif ftype == "group":
                self.walk(f["fields"], self._name_of(f, prefix))
            else:
                self._do_leaf(f, prefix)

    @staticmethod
    def _name_of(f: dict, prefix: str) -> str:
        return f"{prefix}.{f['name']}" if prefix else f["name"]

    def _eval_when(self, when: dict, where: str) -> bool:
        v = self.resolve_value(when["field"], where)
        op = when["op"]
        expected = when.get("value")
        if op in ("in", "notin"):
            if not isinstance(expected, list):
                raise ParseError(f"{where}: {op} 操作要求 value 为数组")
        return bool(OPS[op](v, expected))

    # -- switch 分支 ------------------------------------------------------

    def _do_switch(self, f: dict, prefix: str) -> None:
        path = self._name_of(f, prefix)
        raw = self.resolve_value(f["selector"], path)
        leaf = self._find_leaf(f["selector"], path)
        label = self._label_for(leaf)

        chosen_idx: int | None = None
        chosen_case: dict | None = None
        for i, case in enumerate(f["cases"]):
            if "match" in case:
                keys = case["match"] if isinstance(case["match"], list) else [case["match"]]
                keys = [self._case_key(k) for k in keys]
                if raw in keys:
                    chosen_idx, chosen_case = i, case
                    break
            elif "when" in case:
                if self._eval_case_when(case["when"], raw):
                    chosen_idx, chosen_case = i, case
                    break

        self.result.branch_path.append({
            "switch": path,
            "selector": f["selector"],
            "selector_value": raw,
            "selector_enum_label": label,
            "taken_case": chosen_case.get("name") if chosen_case else None,
            "taken_case_index": chosen_idx,
            "default_taken": bool(chosen_case and "default" in chosen_case and chosen_case["default"]),
            "cases": [
                {"index": i,
                 "name": c.get("name"),
                 "taken": i == chosen_idx,
                 "is_default": bool(c.get("default"))}
                for i, c in enumerate(f["cases"])
            ],
        })

        if chosen_case is None:
            if f.get("optional"):
                return
            raise ParseError(f"{path}: selector 值 {raw} 未匹配任何分支")
        self.walk(chosen_case["fields"], f"{path}.{chosen_case.get('name', chosen_idx)}")

    @staticmethod
    def _case_key(key: Any) -> int:
        if isinstance(key, bool):
            raise ParseError("switch match 不能是布尔")
        if isinstance(key, int):
            return key
        if isinstance(key, str) and key.startswith(("0x", "0X")):
            return int(key, 16)
        return int(key)

    def _eval_case_when(self, when: dict, raw: int) -> bool:
        op = when["op"]
        if op not in OPS:
            raise ParseError(f"case when 非法操作符 {op}")
        return bool(OPS[op](raw, when.get("value")))

    # -- 叶子字段 ---------------------------------------------------------

    def _do_leaf(self, f: dict, prefix: str) -> None:
        path = self._name_of(f, prefix)
        bits = f["bits"]
        endian = f.get("endian", self.default_endian)
        bit_offset = self.cursor
        ftype = f["type"]
        raw = None
        value: Any = None
        enum_label = None
        note = None

        if ftype != "checksum":
            raw = _read_bits(self.data, self.cursor, bits, endian)

        if ftype == "padding":
            raw = None
            value = None
            note = "填充位"
        elif ftype == "bool":
            value = bool(raw)
        elif ftype == "int":
            value = _twos_complement(raw, bits)
        elif ftype == "enum":
            value = raw
            values = f["values"]
            enum_label = values.get(str(raw), values.get(raw))
        elif ftype == "fixed":
            value = raw
            if raw != f["value"]:
                self.result.errors.append(
                    f"{path}: fixed 常量不匹配，期望 {_hex_value(f['value'], bits)}，"
                    f"实际 {_hex_value(raw, bits)}")
        elif ftype == "checksum":
            self.cursor += bits
            self._verify_checksum(f, path, bit_offset, bits, endian)
            return
        else:  # uint
            value = raw

        pf = ParsedField(
            name=path, type=ftype, bit_offset=bit_offset, bits=bits,
            value=value, raw_hex=_hex_value(raw, bits) if raw is not None else None,
            endian=endian, enum_label=enum_label, note=note)
        self.result.fields.append(pf)
        self.leaves.append({"name": path, "value": value if value is not None else raw,
                            "bit_offset": bit_offset, "def": f})
        self.cursor += bits

    # -- 校验和 -----------------------------------------------------------

    def _verify_checksum(self, f: dict, path: str, bit_offset: int,
                         bits: int, endian: str) -> None:
        algorithm = f["algorithm"]
        cover = f.get("cover", "before")
        total = len(self.data) * 8

        if cover == "before":
            start, end = 0, bit_offset
            range_text = f"0:{bit_offset}"
        elif cover == "all_except":
            start, end = 0, total
            range_text = f"all bytes except {path}"
        else:
            start, end = cover["start_bit"], cover["end_bit"]
            range_text = f"{start}:{end}"

        if start % 8 or end % 8:
            raise ParseError(f"{path}: checksum 覆盖范围必须字节对齐")
        if not 0 <= start <= end <= total:
            raise ParseError(f"{path}: checksum 覆盖范围超出报文长度")

        expected = _read_bits(self.data, bit_offset, bits, endian)
        covered = bytearray(self.data[:total // 8])
        if cover == "all_except":
            cs_start, cs_end = bit_offset // 8, (bit_offset + bits) // 8
            covered = bytearray(self.data[:cs_start] + self.data[cs_end:])
        else:
            covered = bytearray(self.data[start // 8:end // 8])

        actual = _compute_checksum(bytes(covered), algorithm, f.get("crc_params"))
        passed = actual == expected

        self.result.checksums.append(ChecksumResult(
            field=path, algorithm=algorithm, expected=expected, actual=actual,
            passed=passed, covered_bytes=range_text,
            bit_offset=bit_offset, bits=bits))

        self.result.fields.append(ParsedField(
            name=path, type="checksum", bit_offset=bit_offset, bits=bits,
            value=expected, raw_hex=_hex_value(expected, bits), endian=endian,
            note=f"{algorithm} {'通过' if passed else '不通过'}"))
        self.leaves.append({"name": path, "value": expected,
                            "bit_offset": bit_offset, "def": f})
        if not passed:
            width = bits // 4
            self.result.errors.append(
                f"{path}: {algorithm} 校验失败，"
                f"期望 0x{expected:0{width}X}，实际 0x{actual:0{width}X}")


# ---------------------------------------------------------------------------
# 版本与指纹
# ---------------------------------------------------------------------------

def version_key(v: str) -> tuple[int, ...]:
    """语义版本号转可比较元组，支持 1 / 1.2 / 1.2.3。"""
    try:
        parts = [int(p) for p in v.strip().split(".")]
    except (ValueError, AttributeError) as e:
        raise DefinitionError(f"非法版本号 {v!r}，应为如 1.2.3 的数字版本") from e
    if not parts or len(parts) > 3 or any(p < 0 for p in parts):
        raise DefinitionError(f"非法版本号 {v!r}")
    return tuple(parts)


def definition_fingerprint(definition: dict[str, Any]) -> str:
    """对规范化后的定义生成指纹（SHA256 前 16 位），用于检索/审计。"""
    import hashlib
    canonical = json.dumps(definition, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
