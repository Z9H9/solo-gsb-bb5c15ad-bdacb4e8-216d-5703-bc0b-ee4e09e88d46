"""解析器单元测试（纯标准库，可直接 python -m unittest）。"""
import unittest

from parser import (DefinitionError, ParseError, _compute_checksum,
                    definition_fingerprint, parse_message, validate_definition,
                    version_key)


def crc16_modbus(data: bytes) -> int:
    return _compute_checksum(data, "crc16/modbus", None)


class TestChecksum(unittest.TestCase):
    def test_crc16_modbus_known_vector(self):
        # 01 03 00 00 00 0A -> CRC16/MODBUS 寄存器值 = CD C5，
        # 线上小端字节为 C5 CD
        self.assertEqual(crc16_modbus(bytes.fromhex("01030000000A")), 0xCDC5)

    def test_crc8_smbus(self):
        self.assertEqual(_compute_checksum(b"\x00", "crc8/smbus", None), 0x00)

    def test_xor_and_sum(self):
        self.assertEqual(_compute_checksum(b"\x01\x02\x03", "xor8", None), 0x00)
        self.assertEqual(_compute_checksum(b"\xff\x02", "sum8", None), 0x01)
        self.assertEqual(_compute_checksum(b"\x01\x02", "checksum8", None), 0x03)
        self.assertEqual(_compute_checksum(b"\xff\x02", "sum16", None), 0x0101)

    def test_custom_crc_params(self):
        # CRC-16/MODBUS 预置等价于自定义参数
        p = dict(poly=0x8005, init=0xFFFF, refin=True, refout=True, xorout=0)
        self.assertEqual(_compute_checksum(b"\x01\x03", "crc16/ccitt-false", p),
                         crc16_modbus(b"\x01\x03"))


SIMPLE = {
    "endian": "big",
    "fields": [
        {"name": "a", "type": "uint", "bits": 8},
        {"name": "b", "type": "int", "bits": 8, "endian": "little"},
        {"name": "c", "type": "bool", "bits": 1},
        {"name": "kind", "type": "enum", "bits": 3, "values": {"1": "x", "2": "y"}},
        {"name": "pad", "type": "padding", "bits": 4},
        {"name": "cs", "type": "checksum", "bits": 8, "algorithm": "sum8",
         "cover": "before"},
    ],
}


class TestParsing(unittest.TestCase):
    def test_bit_field_and_sum8(self):
        # a=01, b=FE(-2), c=1, kind=1 -> 第三字节 0b10010000=90; pad=00;
        # sum(01+FE+90)=18F -> 8F（pad 在 cs 之前的高半字节）
        r = parse_message(SIMPLE, "01FE908F")
        self.assertTrue(r.valid, r.errors)
        names = [(f.name, f.bit_offset, f.bits) for f in r.fields]
        self.assertEqual(names[:4], [
            ("a", 0, 8), ("b", 8, 8), ("c", 16, 1), ("kind", 17, 3)])
        self.assertEqual(r.fields[0].value, 1)
        self.assertEqual(r.fields[1].value, -2)
        self.assertIs(r.fields[2].value, True)
        self.assertEqual(r.fields[3].value, 1)
        self.assertEqual(r.fields[3].enum_label, "x")
        self.assertTrue(r.checksums[0].passed)

    def test_bad_checksum(self):
        r = parse_message(SIMPLE, "01FE9000")
        self.assertFalse(r.valid)
        self.assertTrue(any("校验失败" in e for e in r.errors))
        self.assertFalse(r.checksums[0].passed)
        self.assertEqual(r.checksums[0].actual, 0x8F)

    def test_truncated(self):
        with self.assertRaises(ParseError):
            parse_message(SIMPLE, "01FE")

    def test_trailing_bytes(self):
        r = parse_message(SIMPLE, "01FE908F00")
        self.assertFalse(r.valid)
        self.assertTrue(any("尾字节" in e for e in r.errors))

    def test_bad_hex(self):
        with self.assertRaises(ParseError):
            parse_message(SIMPLE, "zzzz")

    def test_fixed_mismatch(self):
        d = {"endian": "big", "fields": [
            {"name": "f", "type": "fixed", "bits": 8, "value": 255},
            {"name": "cs", "type": "checksum", "bits": 8,
             "algorithm": "sum8", "cover": "before"}]}
        r = parse_message(d, "0000")
        self.assertFalse(r.valid)
        self.assertTrue(any("fixed 常量不匹配" in e for e in r.errors))


SWITCH_DEF = {
    "endian": "big",
    "fields": [
        {"name": "addr", "type": "uint", "bits": 8},
        {"name": "mode", "type": "enum", "bits": 8,
         "values": {"0": "poll", "1": "event"}},
        {"name": "payload", "type": "switch", "selector": "mode", "cases": [
            {"name": "p", "match": 0, "fields": [
                {"name": "reg", "type": "uint", "bits": 16}]},
            {"name": "e", "match": 1, "fields": [
                {"name": "temp", "type": "int", "bits": 16, "endian": "little"}]},
        ]},
        {"name": "crc", "type": "checksum", "bits": 16,
         "algorithm": "crc16/modbus", "endian": "little", "cover": "before"},
    ],
}


class TestSwitch(unittest.TestCase):
    def _msg(self, head: bytes) -> str:
        return (head + crc16_modbus(head).to_bytes(2, "little")).hex()

    def test_poll_branch(self):
        r = parse_message(SWITCH_DEF, self._msg(bytes.fromhex("01000010")))
        self.assertTrue(r.valid, r.errors)
        bp = r.branch_path[0]
        self.assertEqual(bp["taken_case"], "p")
        self.assertEqual(bp["selector_value"], 0)
        self.assertEqual(bp["selector_enum_label"], "poll")
        vals = {f.name: f.value for f in r.fields if f.taken}
        self.assertEqual(vals["payload.p.reg"], 0x10)
        self.assertNotIn("payload.e.temp", vals)

    def test_event_branch_little_endian_negative(self):
        # temp = -100 -> 0xFF9C, little endian 字节为 9C FF
        r = parse_message(SWITCH_DEF, self._msg(bytes.fromhex("02019CFF")))
        self.assertTrue(r.valid, r.errors)
        self.assertEqual(r.branch_path[0]["taken_case"], "e")
        vals = {f.name: f.value for f in r.fields if f.taken}
        self.assertEqual(vals["payload.e.temp"], -100)

    def test_unmatched_branch_non_optional(self):
        with self.assertRaises(ParseError):
            parse_message(SWITCH_DEF, self._msg(bytes.fromhex("01090010")))

    def test_optional_switch_ok(self):
        d = {"endian": "big", "fields": [
            {"name": "m", "type": "uint", "bits": 8},
            {"name": "s", "type": "switch", "selector": "m", "optional": True,
             "cases": [{"name": "a", "match": 1, "fields": [
                 {"name": "x", "type": "uint", "bits": 8}]}]},
            {"name": "cs", "type": "checksum", "bits": 8,
             "algorithm": "sum8", "cover": "before"}]}
        r = parse_message(d, "0000")  # m=0 不匹配任何分支，整包仅 2 字节
        self.assertTrue(r.valid, r.errors)
        self.assertIsNone(r.branch_path[0]["taken_case"])


class TestWhen(unittest.TestCase):
    DEF = {
        "endian": "big",
        "fields": [
            {"name": "flags", "type": "uint", "bits": 8},
            {"name": "ext", "type": "uint", "bits": 8,
             "when": {"field": "flags", "op": "==", "value": 1}},
            {"name": "cs", "type": "checksum", "bits": 8,
             "algorithm": "sum8", "cover": "before"},
        ],
    }

    def test_when_taken_and_skipped(self):
        r1 = parse_message(self.DEF, "010203")  # flags=1 -> ext 存在
        self.assertTrue(r1.valid, r1.errors)
        self.assertTrue(any(f.name == "ext" and f.taken for f in r1.fields))

        r2 = parse_message(self.DEF, "0000")  # flags=0 -> ext 跳过，2 字节
        self.assertTrue(r2.valid, r2.errors)
        ext = [f for f in r2.fields if f.name == "ext"][0]
        self.assertFalse(ext.taken)

    def test_in_operator(self):
        d = {"endian": "big", "fields": [
            {"name": "k", "type": "uint", "bits": 8},
            {"name": "v", "type": "uint", "bits": 8,
             "when": {"field": "k", "op": "in", "value": [1, 2, 3]}}]}
        self.assertTrue(parse_message(d, "02FF").valid)
        r = parse_message(d, "09")
        self.assertTrue(r.valid)
        self.assertFalse([f for f in r.fields if f.name == "v"][0].taken)


class TestCoverModes(unittest.TestCase):
    def test_all_except(self):
        d = {"endian": "big", "fields": [
            {"name": "h", "type": "uint", "bits": 8},
            {"name": "cs", "type": "checksum", "bits": 8,
             "algorithm": "xor8", "cover": "all_except"},
            {"name": "t", "type": "uint", "bits": 8}]}
        # xor(h, t) = xor(10, 20) = 30
        r = parse_message(d, "103020")
        self.assertTrue(r.valid, r.errors)
        self.assertEqual(r.checksums[0].covered_bytes,
                         "all bytes except cs")

    def test_explicit_range(self):
        d = {"endian": "big", "fields": [
            {"name": "h", "type": "uint", "bits": 8},
            {"name": "cs", "type": "checksum", "bits": 8, "algorithm": "sum8",
             "cover": {"start_bit": 16, "end_bit": 32}},
            {"name": "a", "type": "uint", "bits": 8},
            {"name": "b", "type": "uint", "bits": 8}]}
        r = parse_message(d, "00080305")  # cs = a+b = 08
        self.assertTrue(r.valid, r.errors)

    def test_misaligned_checksum_range(self):
        d = {"endian": "big", "fields": [
            {"name": "x", "type": "uint", "bits": 4},
            {"name": "y", "type": "uint", "bits": 4},
            {"name": "z", "type": "uint", "bits": 4},
            {"name": "cs", "type": "checksum", "bits": 8,
             "algorithm": "sum8"}]}
        # cs 起始于 bit 12，未字节对齐
        with self.assertRaises(ParseError):
            parse_message(d, "102000")


class TestValidation(unittest.TestCase):
    def test_bad_definitions(self):
        bad_cases = [
            {},
            {"endian": "middle", "fields": [{"name": "a", "type": "uint", "bits": 8}]},
            {"fields": []},
            {"fields": [{"name": "a", "type": "uint"}]},
            {"fields": [{"name": "a", "type": "bool", "bits": 2}]},
            {"fields": [{"name": "a", "type": "enum", "bits": 8}]},
            {"fields": [{"name": "a", "type": "checksum", "bits": 16,
                         "algorithm": "xor8"}]},
            {"fields": [{"name": "a", "type": "checksum", "bits": 8,
                         "algorithm": "nope"}]},
            {"fields": [{"name": "a", "type": "checksum", "bits": 4,
                         "algorithm": "xor8"}]},
            {"fields": [{"name": "a", "type": "switch"}]},
            {"fields": [{"name": "a", "type": "switch", "selector": "x",
                         "cases": []}]},
            {"fields": [{"name": "a", "type": "group"}]},
            {"fields": [{"name": "a", "type": "fixed", "bits": 8}]},
            {"fields": [{"name": "a", "type": "fixed", "bits": 8, "value": 999}]},
        ]
        for d in bad_cases:
            with self.assertRaises(DefinitionError, msg=d):
                validate_definition(d)

    def test_nested_switch_validation(self):
        d = {"fields": [{"name": "s", "type": "switch", "selector": "m",
                         "cases": [{"name": "c", "fields": [
                             {"name": "x", "type": "bad", "bits": 8}]}]}]}
        with self.assertRaises(DefinitionError):
            validate_definition(d)


class TestVersion(unittest.TestCase):
    def test_ordering(self):
        self.assertTrue(version_key("1.10.0") > version_key("1.9.9"))
        self.assertTrue(version_key("2") > version_key("1.9"))
        with self.assertRaises(DefinitionError):
            version_key("1.x")

    def test_fingerprint_stable(self):
        d = {"fields": [{"name": "a", "type": "uint", "bits": 8}]}
        self.assertEqual(definition_fingerprint(d), definition_fingerprint(d))


if __name__ == "__main__":
    unittest.main()


class TestGroupAndMisc(unittest.TestCase):
    def test_group_namespace_and_int_enum_keys(self):
        d = {"endian": "big", "fields": [
            {"name": "h", "type": "group", "fields": [
                {"name": "kind", "type": "enum", "bits": 8,
                 "values": {7: "seven"}},
                {"name": "flag", "type": "bool", "bits": 1},
                {"name": "v", "type": "uint", "bits": 7}]},
            {"name": "tail", "type": "uint", "bits": 8,
             "when": {"field": "h.kind", "op": "==", "value": 7}}]}
        # kind=7, flag=1, v=0x01 -> 0x81; tail 存在 = 0xFF（共 3 字节）
        r = parse_message(d, "0781FF")
        self.assertTrue(r.valid, r.errors)
        vals = {f.name: f.value for f in r.fields if f.taken}
        self.assertEqual(vals["h.kind"], 7)
        self.assertEqual([f.enum_label for f in r.fields if f.name == "h.kind"][0],
                         "seven")
        self.assertIs(vals["h.flag"], True)
        self.assertEqual(vals["h.v"], 1)
        self.assertEqual(vals["tail"], 0xFF)

        # kind != 7 -> tail 跳过
        r2 = parse_message(d, "0000")
        self.assertTrue(r2.valid, r2.errors)
        self.assertFalse([f for f in r2.fields if f.name == "tail"][0].taken)

    def test_nested_switch_selector_full_path(self):
        d = {"endian": "big", "fields": [
            {"name": "m", "type": "uint", "bits": 8},
            {"name": "outer", "type": "switch", "selector": "m", "cases": [
                {"name": "a", "match": 0, "fields": [
                    {"name": "m_in", "type": "uint", "bits": 8},
                    {"name": "inner", "type": "switch",
                     "selector": "outer.a.m_in", "cases": [
                        {"name": "lo", "match": 1, "fields": [
                            {"name": "x", "type": "uint", "bits": 8}]}]}]}]},
            {"name": "cs", "type": "checksum", "bits": 8,
             "algorithm": "sum8", "cover": "before"}]}
        r = parse_message(d, "00010203")  # m=0 -> a; m_in=1 -> lo; x=2
        self.assertTrue(r.valid, r.errors)
        self.assertEqual(r.branch_path[1]["taken_case"], "lo")
        self.assertEqual(
            [f.value for f in r.fields if f.name == "outer.a.inner.lo.x"][0], 2)
