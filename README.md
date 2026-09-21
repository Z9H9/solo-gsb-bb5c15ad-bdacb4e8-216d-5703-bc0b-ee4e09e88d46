# 工业二进制报文解析 API

基于 **Python + FastAPI + SQLite** 的工业二进制报文按位解析服务，**Docker Compose 一键启动**。

注册“带版本”的报文定义（位宽、端序、条件分支、校验字段），提交指定版本的十六进制报文后，服务按 bit 位解析、校验校验和，并返回：

- 每个字段的**值**、**位偏移（bit_offset）**、占用位数、原始十六进制；
- switch 的**实际分支路径**（命中哪个 case、selector 取值、枚举标签）；
- 校验和的**期望值 / 实际值 / 是否通过 / 覆盖字节范围**。

数据使用 SQLite 持久化（命名卷），服务首次启动时**自动建表初始化**，无需手工迁移。

---

## 1. 一键启动

```bash
docker compose up -d --build
```

服务监听 **8080** 端口。验证：

```bash
curl http://localhost:8080/health
# {"status":"ok"}
```

停止：

```bash
docker compose down          # 保留数据
docker compose down -v       # 同时删除数据库卷
```

> 不使用 Docker 时的本地运行：`pip install -r requirements.txt`，然后
> `DB_PATH=./data/parser.db uvicorn main:app --host 0.0.0.0 --port 8080`

交互式 API 文档（Swagger UI）：<http://localhost:8080/docs>

---

## 2. 配置

通过 docker-compose.yml 的环境变量配置：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH` | `/data/parser.db` | SQLite 数据库文件路径，容器内挂载到命名卷 `parser-data` |
| `PORT` | `8080` | 容器监听端口（同时需同步 `ports` 映射） |

宿主机端口在 `docker-compose.yml` 的 `ports: "宿主端口:8080"` 中修改。

---

## 3. curl 示例

### 3.1 注册报文定义（带版本）

```bash
curl -s -X POST http://localhost:8080/api/v1/definitions \
  -H 'Content-Type: application/json' \
  -d @examples/sensor-ht.v1.0.0.json
```

或内联 JSON：

```bash
curl -s -X POST http://localhost:8080/api/v1/definitions \
  -H 'Content-Type: application/json' -d '{
  "device_type": "sensor-ht",
  "version": "1.0.0",
  "description": "温湿度传感器示例",
  "definition": {
    "endian": "big",
    "fields": [
      {"name": "address",  "type": "uint",  "bits": 8},
      {"name": "function", "type": "fixed", "bits": 8, "value": 3},
      {"name": "mode", "type": "enum", "bits": 4,
       "values": {"0": "poll", "1": "event"}},
      {"name": "reserved", "type": "padding", "bits": 4},
      {"name": "payload", "type": "switch", "selector": "mode",
       "cases": [
         {"name": "poll_request", "match": 0, "fields": [
           {"name": "register", "type": "uint", "bits": 16},
           {"name": "count",    "type": "uint", "bits": 8}]},
         {"name": "event_report", "match": 1, "fields": [
           {"name": "status", "type": "enum", "bits": 8,
            "values": {"0": "ok", "1": "alarm"}},
           {"name": "battery_percent", "type": "uint", "bits": 8},
           {"name": "temperature", "type": "int", "bits": 16,
            "endian": "little"}]}
       ]},
      {"name": "crc", "type": "checksum", "bits": 16,
       "algorithm": "crc16/modbus", "endian": "little", "cover": "before"}
    ]
  }
}'
```

同一 `device_type + version` 重复注册返回 **409**；定义结构不合法返回 **422**。

### 3.2 解析报文（轮询分支，CRC 正确）

```bash
curl -s -X POST http://localhost:8080/api/v1/parse \
  -H 'Content-Type: application/json' \
  -d '{"device_type":"sensor-ht","version":"1.0.0",
       "hex_payload":"01 03 00 00 10 01 89 ca"}'
```

十六进制串允许空格、`0x` 前缀。返回（节选）：

```json
{
  "device_type": "sensor-ht",
  "version": "1.0.0",
  "parse": {
    "valid": true,
    "byte_length": 8,
    "fields": [
      {"name": "address", "type": "uint", "bit_offset": 0,  "bits": 8,  "value": 1,  "raw_hex": "0x01"},
      {"name": "mode",    "type": "enum", "bit_offset": 16, "bits": 4,  "value": 0,  "enum_label": "poll"},
      {"name": "payload.poll_request.register", "bit_offset": 24, "bits": 16, "value": 16, "raw_hex": "0x0010"},
      {"name": "payload.poll_request.count",    "bit_offset": 40, "bits": 8,  "value": 1},
      {"name": "crc", "type": "checksum", "bit_offset": 48, "bits": 16, "value": 51849,
       "note": "crc16/modbus 通过"}
    ],
    "branch_path": [{
      "switch": "payload", "selector": "mode", "selector_value": 0,
      "selector_enum_label": "poll", "taken_case": "poll_request", "taken_case_index": 0
    }],
    "checksums": [{
      "field": "crc", "algorithm": "crc16/modbus",
      "expected_hex": "0xCA89", "actual_hex": "0xCA89", "passed": true,
      "covered_byte_range": "0:48"
    }],
    "errors": []
  }
}
```

事件分支示例（temperature 为小端有符号数 -100）：

```bash
curl -s -X POST http://localhost:8080/api/v1/parse \
  -H 'Content-Type: application/json' \
  -d '{"device_type":"sensor-ht","version":"1.0.0",
       "hex_payload":"01031000649cffa2af"}'
# branch_path[0].taken_case = "event_report"
# payload.event_report.temperature = -100
```

报文**结构**无法解析（长度不足、字段未对齐等）返回 HTTP **422**；报文能解析但**内容校验失败**（校验和错误、固定常量不符、尾字节多余）返回 HTTP **200** 且 `parse.valid = false`，失败原因见 `parse.errors`：

```bash
curl -s -X POST http://localhost:8080/api/v1/parse \
  -H 'Content-Type: application/json' \
  -d '{"device_type":"sensor-ht","version":"1.0.0","hex_payload":"0103000010010000"}'
# parse.valid = false
# parse.errors = ["crc: crc16/modbus 校验失败，期望 0x0000，实际 0xCA89"]
```

### 3.3 列出定义 / 按设备类型查询最新版本 / 停用

```bash
# 列出全部定义（可加 ?device_type=sensor-ht&active=true 过滤）
curl -s http://localhost:8080/api/v1/definitions

# 按设备类型查询语义版本号最新（默认仅启用版本）
curl -s http://localhost:8080/api/v1/definitions/sensor-ht/latest
# 包含已停用版本：?include_inactive=true

# 获取指定版本
curl -s http://localhost:8080/api/v1/definitions/sensor-ht/1.0.0

# 停用定义（幂等；停用后该版本不可再用于解析，返回 409）
curl -s -X POST http://localhost:8080/api/v1/definitions/sensor-ht/1.0.0/deactivate
```

版本号采用语义化比较（`1.10.0 > 1.2.1 > 1.0.0`，支持 `1` / `1.2` / `1.2.3`）。

---

## 4. 报文定义规范

顶层结构：

```json
{
  "endian": "big",          // 全局默认端序：big（默认）| little
  "fields": [ /* 字段数组，按位顺序排列 */ ]
}
```

### 4.1 字段类型

| type | 必需属性 | 说明 |
| --- | --- | --- |
| `uint` | `bits` | 无符号整数 |
| `int` | `bits` | 有符号整数（二进制补码） |
| `bool` | `bits=1` | 布尔位 |
| `enum` | `bits`, `values` | 整数 + 标签映射，如 `"values": {"0":"poll","1":"event"}` |
| `fixed` | `bits`, `value` | 固定常量字段，值不符时校验失败 |
| `padding` | `bits` | 填充位，只占位数不产生值 |
| `checksum` | `bits`, `algorithm` | 校验和字段，见下节 |
| `switch` | `selector`, `cases` | 条件分支 |
| `group` | `fields` | 字段分组（命名空间，便于组织） |

通用属性：

- `bits`：字段位宽（正整数），字段按声明顺序紧密打包，`bit_offset` 从 0 开始累计；
- `endian`：可在任一字段上覆盖全局端序（`big` / `little`）；
  - 完全字节对齐的多字节字段按该端序解释；
  - 跨字节位域按 MSB-first 连续取位（工业协议常见的“高位在前”打包）；
- `when`：字段级条件，仅当引用字段满足条件时该字段才存在：
  `{"field": "flags", "op": "==", "value": 1}`，
  支持 `== != > >= < <= in notin`（`in/notin` 的 value 为数组）。被跳过的字段在响应中 `taken=false`。

### 4.2 条件分支（switch）

```json
{"name": "payload", "type": "switch", "selector": "mode", "optional": false,
 "cases": [
   {"name": "poll_request", "match": 0, "fields": [ ... ]},
   {"name": "event_report", "match": [1, 2], "fields": [ ... ]},
   {"name": "expr_case", "when": {"op": ">=", "value": 128}, "fields": [ ... ]},
   {"name": "fallback", "default": true, "fields": [ ... ]}
 ]}
```

- `selector` 引用**此前已解析**的字段（分支内字段可用完整路径，如 `payload.poll_request.count`）；
- `match` 为单个整数或整数数组（精确匹配）；`when` 为表达式匹配；`default` 兜底；
- 无任何 case 命中时：`optional=true` 则跳过整个 switch，否则解析报错；
- 解析结果 `branch_path` 记录每个 switch 的 selector 值、枚举标签、命中 case 及全部 case 的命中情况。

### 4.3 校验和字段

```json
{"name": "crc", "type": "checksum", "bits": 16,
 "algorithm": "crc16/modbus", "endian": "little", "cover": "before"}
```

| algorithm | bits | 说明 |
| --- | --- | --- |
| `xor8` | 8 | 逐字节异或 |
| `checksum8` / `sum8` | 8 | 逐字节求和 mod 256 |
| `sum16` | 16 | 逐字节求和 mod 65536 |
| `crc8` / `crc8/smbus` | 8 | CRC-8（poly 0x07，init 0x00） |
| `crc16/modbus` | 16 | CRC-16/MODBUS（poly 0x8005，init 0xFFFF，反射） |
| `crc16/ccitt-false` | 16 | CRC-16/CCITT-FALSE（poly 0x1021，init 0x0000） |

- 校验和字段必须**字节对齐**，`endian` 决定其在线路上的字节序（如 MODBUS CRC 为小端）；
- `cover` 覆盖范围：
  - `"before"`（默认）：校验字段之前的所有字节；
  - `"all_except"`：除校验字段外的整包所有字节；
  - `{"start_bit": 16, "end_bit": 48}`：自定义 bit 区间（须字节对齐）；
- 自定义 CRC 参数可加 `crc_params`：
  `{"poly":8193,"init":0,"refin":false,"refout":false,"xorout":0}`。

### 4.4 错误语义

| 场景 | HTTP 状态 | 响应 |
| --- | --- | --- |
| 定义结构非法（注册时） | 422 | `detail` 说明具体字段问题 |
| `device_type+version` 重复 | 409 | — |
| 版本号非法 | 422 | — |
| 定义不存在 | 404 | — |
| 使用已停用定义解析 | 409 | — |
| 报文结构错误（截断、非对齐、无匹配分支） | 422 | — |
| 解析完成但校验失败（CRC/固定值/尾字节） | 200 | `parse.valid=false` + `parse.errors` |

---

## 5. API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/api/v1/definitions` | 注册带版本的报文定义 |
| GET | `/api/v1/definitions` | 列出定义（`device_type`、`active` 过滤） |
| GET | `/api/v1/definitions/{device_type}/latest` | 查询最新版本（`include_inactive`） |
| GET | `/api/v1/definitions/{device_type}/{version}` | 获取指定版本 |
| POST | `/api/v1/definitions/{device_type}/{version}/deactivate` | 停用定义 |
| POST | `/api/v1/parse` | 按指定版本解析十六进制报文 |

---

## 6. 项目结构与测试

```
main.py        # FastAPI 路由与错误处理
parser.py      # 按位解析 / 分支 / 校验和（纯标准库）
db.py          # SQLite 持久化与自动建表
schemas.py     # Pydantic 请求/响应模型
examples/      # 示例报文定义
tests/         # 43 个单元/集成测试（python -m unittest discover -s tests）
```

本地运行测试：

```bash
pip install -r requirements.txt httpx
python -m unittest discover -s tests -v
```

SQLite 文件位于 Docker 命名卷 `parser-data`（容器内 `/data/parser.db`），容器重建后数据不丢失；表结构在启动时通过幂等 `CREATE TABLE IF NOT EXISTS` 自动初始化。
