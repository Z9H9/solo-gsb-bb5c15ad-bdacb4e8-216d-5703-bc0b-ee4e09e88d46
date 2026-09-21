"""持久化层测试（标准库，临时 SQLite 文件）。"""
import os
import tempfile
import unittest

from db import Database
from parser import DefinitionError

DEF_V1 = {"endian": "big", "fields": [
    {"name": "a", "type": "uint", "bits": 8}]}
DEF_V2 = {"endian": "big", "fields": [
    {"name": "a", "type": "uint", "bits": 8},
    {"name": "b", "type": "uint", "bits": 8}]}


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "sub", "parser.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_and_get(self):
        row = self.db.create_definition("dev", "1.0.0", DEF_V1, "d")
        self.assertTrue(row["active"])
        self.assertEqual(len(row["fingerprint"]), 16)
        got = self.db.get_definition("dev", "1.0.0")
        self.assertEqual(got["definition"], DEF_V1)

    def test_duplicate_rejected(self):
        self.db.create_definition("dev", "1.0.0", DEF_V1)
        with self.assertRaises(DefinitionError):
            self.db.create_definition("dev", "1.0.0", DEF_V2)

    def test_deactivate(self):
        self.db.create_definition("dev", "1.0.0", DEF_V1)
        out = self.db.set_active("dev", "1.0.0", False)
        self.assertFalse(out["active"])
        # 幂等
        self.assertFalse(self.db.set_active("dev", "1.0.0", False)["active"])
        self.assertIsNone(self.db.set_active("nope", "9.9.9", False))
        actives = self.db.list_definitions("dev", active=True)
        self.assertEqual(actives, [])

    def test_latest_by_semver(self):
        for v in ["1.0.0", "1.10.0", "1.2.1", "2.0"]:
            self.db.create_definition("dev", v, DEF_V1)
        latest = self.db.get_latest_version("dev")
        self.assertEqual(latest["version"], "2.0")
        self.db.set_active("dev", "2.0", False)
        latest = self.db.get_latest_version("dev")
        self.assertEqual(latest["version"], "1.10.0")
        latest_all = self.db.get_latest_version("dev", active_only=False)
        self.assertEqual(latest_all["version"], "2.0")
        self.assertIsNone(self.db.get_latest_version("ghost"))

    def test_list_filter(self):
        self.db.create_definition("a", "1.0.0", DEF_V1)
        self.db.create_definition("b", "1.0.0", DEF_V1)
        self.db.set_active("a", "1.0.0", False)
        self.assertEqual(len(self.db.list_definitions()), 2)
        self.assertEqual(len(self.db.list_definitions(active=True)), 1)
        self.assertEqual(self.db.list_definitions("a")[0]["device_type"], "a")

    def test_persistence_across_connections(self):
        path = os.path.join(self.tmp.name, "p.db")
        Database(path).create_definition("dev", "1.0.0", DEF_V1)
        # 重新打开同一文件（模拟容器重启），自动建表 + 数据仍在
        self.assertIsNotNone(Database(path).get_definition("dev", "1.0.0"))


if __name__ == "__main__":
    unittest.main()
