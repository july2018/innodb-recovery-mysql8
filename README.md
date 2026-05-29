# InnoDB Recovery Tool for MySQL 8.0

> 模仿 [undrop-for-innodb](https://github.com/twindb/undrop-for-innodb) 的思路，  
> 结合 MySQL 8.0 InnoDB 源码（`storage/innobase/`），  
> 实现对 MySQL 8.0 `.ibd` 文件的数据恢复。

---

## 背景

`undrop-for-innodb` 不支持 MySQL 8.0，原因：

| 变化点 | MySQL 5.7 | MySQL 8.0 |
|--------|-----------|-----------|
| 字典存储 | `.frm` 文件 | SDI 页（FIL_PAGE_SDI=0x0045）|
| Instant ADD COLUMN | 无 | `REC_INFO_INSTANT_FLAG`(0x80) / `REC_INFO_VERSION_FLAG`(0x40) |
| 行版本号 | 无 | 数据区前置 version byte |
| Checksum 算法 | innodb_fast_checksum | 默认 crc32 |
| 可变字段长度编码 | 1~2 字节 | 同，但 instant 版本有扩展 |

本工具从 MySQL 8.0 源码中提取关键常量与结构，完整支持上述变化。

---

## 核心原理

### 1. InnoDB 页结构（16KB）

```
┌─────────────────────────────────┐  0
│  FIL Header (38 bytes)          │  ← checksum/page_no/prev/next/LSN/type/space_id
├─────────────────────────────────┤  38
│  PAGE Header (56 bytes)         │  ← n_recs/heap_top/free_list/level/index_id
├─────────────────────────────────┤  94
│  Infimum record (13 bytes)      │
│  Supremum record (13 bytes)     │
├─────────────────────────────────┤  120
│  User Records                   │  ← 链表顺序存放，delete-marked 仍在此
│  ...                            │
├─────────────────────────────────┤  heap_top
│  Free Space                     │
├─────────────────────────────────┤
│  Page Directory (slots)         │
├─────────────────────────────────┤  16376
│  FIL Trailer (8 bytes)          │  ← old LSN + checksum
└─────────────────────────────────┘  16384
```

### 2. COMPACT/DYNAMIC 记录格式（MySQL 8.0 new-style）

```
[变长字段长度列表（逆序）] [NULL 位图（逆序）] [5字节 extra] → 数据
                                                  ↑
                          info_bits|n_owned(1B) + heap_no|status(2B) + next_off(2B)

MySQL 8.0 新增：若 REC_INFO_INSTANT_FLAG=1，数据区首字节是 instant_version
```

### 3. 恢复思路

```
读取 .ibd 文件
    ↓
逐页扫描 → 找 FIL_PAGE_INDEX（0x45BF）叶子页（PAGE_LEVEL=0）
    ↓
跟随 infimum→next 链遍历所有记录（含 delete-marked）
    ↓
若链表断裂 → 暴力扫描页内所有偏移
    ↓
解析 NULL 位图 + 变长长度列表 + 系统列 + 用户列
    ↓
输出 INSERT SQL / CSV / JSON
```

---

## 安装与使用

### 环境要求

- Python 3.8+，无需额外依赖

### 快速开始

**Step 1：生成 schema 模板**

```bash
python innodb_recovery.py --gen-schema your_table --schema-out schema.json
```

编辑 `schema.json`，按实际 CREATE TABLE 语句填写列定义：

```json
{
  "table": "orders",
  "row_format": "DYNAMIC",
  "columns": [
    {"name": "id",         "type": "bigint",        "nullable": false, "unsigned": true},
    {"name": "user_id",    "type": "int",            "nullable": false, "unsigned": true},
    {"name": "amount",     "type": "decimal(10,2)",  "nullable": true},
    {"name": "status",     "type": "varchar(32)",    "nullable": true,  "charset": "utf8mb4"},
    {"name": "created_at", "type": "datetime",       "nullable": false}
  ]
}
```

**Step 2：查看 .ibd 文件页信息**

```bash
python innodb_recovery.py --ibd /var/lib/mysql/mydb/orders.ibd --page-info
```

**Step 3：恢复数据**

```bash
# 恢复所有记录（含软删除），输出 SQL
python innodb_recovery.py --ibd orders.ibd --schema schema.json -o recovered.sql

# 仅恢复活跃记录
python innodb_recovery.py --ibd orders.ibd --schema schema.json --no-deleted -o active.sql

# 输出 CSV（含元数据列）
python innodb_recovery.py --ibd orders.ibd --schema schema.json --format csv -o recovered.csv

# 暴力扫描（DROP/TRUNCATE 后页链表可能断裂）
python innodb_recovery.py --ibd orders.ibd --schema schema.json --brute-force -o recovered.sql
```

---

## 参数说明

| 参数 | 说明 |
|------|------|
| `--ibd FILE` | `.ibd` 文件路径（MySQL 8.0 每表一个文件） |
| `--schema FILE` | 表结构 JSON 文件 |
| `-o FILE` | 输出路径（不指定则 stdout） |
| `--format sql\|csv\|json` | 输出格式，默认 sql |
| `--no-deleted` | 排除 delete-marked 记录 |
| `--brute-force` | 暴力扫描模式，适用于链表损坏 |
| `--index-id N` | 只扫描指定 index_id（可从 --page-info 获取）|
| `--page-info` | 打印页类型统计，不执行恢复 |
| `--gen-schema TABLE` | 生成 schema 模板 |
| `--schema-out FILE` | schema 模板输出路径 |
| `-v` | 详细日志 |

---

## Schema 字段说明

```json
{
  "table": "表名",
  "row_format": "DYNAMIC",  // COMPACT / DYNAMIC / REDUNDANT
  "columns": [
    {
      "name": "列名",
      "type": "类型(长度)",    // 如 varchar(255), int, decimal(10,2)
      "nullable": true,        // 是否可为 NULL
      "unsigned": false,       // 整数是否无符号
      "charset": "utf8mb4"     // 字符集（varchar/char/text 有效）
    }
  ]
}
```

**注意：** 不需要包含隐藏系统列（`DB_TRX_ID`、`DB_ROLL_PTR`），工具自动跳过。

---

## MySQL 8.0 特有处理

### Instant ADD COLUMN

MySQL 8.0.29+ 支持 `INSTANT` 算法的 `ALTER TABLE ADD COLUMN`，会在记录中置位：
- `REC_INFO_INSTANT_FLAG (0x80)`：数据区前有 n_fields 字节
- `REC_INFO_VERSION_FLAG (0x40)`：数据区前有 version 字节

工具自动识别并跳过这些前缀字节。

### SDI 页（序列化字典信息）

MySQL 8.0 将表结构信息存入 `.ibd` 文件头部的 SDI 页（类型 `0x0045`）。  
工具自动跳过 SDI 页，不将其当作用户数据页处理。

---

## 典型恢复场景

### 场景一：误执行 DELETE（无 TRUNCATE）

```bash
# 普通扫描即可，delete-marked 记录仍在页中
python innodb_recovery.py --ibd orders.ibd --schema schema.json -o recovered.sql
# 查看 recovered.sql，-- [DELETED] 注释的行即为被删除的记录
```

### 场景二：DROP TABLE / TRUNCATE TABLE

```bash
# 页可能已被复用，但残留数据仍可能存在
# 先看页信息
python innodb_recovery.py --ibd orders.ibd --page-info

# 用暴力扫描
python innodb_recovery.py --ibd orders.ibd --schema schema.json --brute-force -o recovered.sql
```

### 场景三：从 ibdata1 恢复（共享表空间）

```bash
# ibdata1 包含多个表的数据，用 --index-id 指定目标索引
python innodb_recovery.py --ibd ibdata1 --schema schema.json --index-id 123 -o recovered.sql
```

---

## 与 undrop-for-innodb 对比

| 功能 | undrop-for-innodb | 本工具 |
|------|-------------------|--------|
| MySQL 版本 | ≤5.7 | 8.0 |
| 语言 | C | Python 3 |
| 行格式 | COMPACT/DYNAMIC/REDUNDANT | COMPACT/DYNAMIC/REDUNDANT |
| Instant ADD COLUMN | ❌ | ✅ |
| SDI 页处理 | ❌ | ✅ |
| 链式遍历 | ✅ | ✅ |
| 暴力扫描 | ✅（stream_parser）| ✅ |
| 输出格式 | SQL | SQL/CSV/JSON |
| 依赖 | MySQL 客户端库 | 纯 Python 无依赖 |

---

## 源码对应关系

| 本工具 | MySQL 8.0 源码 |
|--------|---------------|
| `FIL_PAGE_*` 常量 | `storage/innobase/include/fil0types.h` |
| `PAGE_*` 常量 | `storage/innobase/include/page0types.h` |
| `REC_*` 常量 | `storage/innobase/rem/rec.h` |
| `REC_INFO_INSTANT_FLAG` | `storage/innobase/rem/rec.h:126` |
| `REC_N_NEW_EXTRA_BYTES=5` | `storage/innobase/rem/rec.h:133` |
| compact 记录解析逻辑 | `storage/innobase/include/rem0rec.ic` |
| REDUNDANT 解析 | `storage/innobase/rem/rem0rec.cc` |

---

## 注意事项

1. **只读操作**：工具不会修改任何文件
2. **停止 MySQL 服务**：建议在操作前停止 MySQL，避免文件被修改
3. **Schema 必须准确**：列顺序、类型、是否 nullable 必须与实际表结构一致
4. **隐式列**：聚簇索引（主键/隐式主键 ROW_ID）的隐藏系统列已自动处理
5. **大对象（LOB）**：BLOB/TEXT 超过页内存储阈值的部分存在外部页，暂不自动追踪

---

## 示例输出（SQL）

```sql
-- MySQL 8.0 InnoDB Recovery Tool
-- Table: orders
-- Recovered: 2026-05-29T08:00:00
-- Total rows: 1523

INSERT INTO `orders` (`id`, `user_id`, `amount`, `status`, `created_at`) VALUES (1, 100, '0x...', 'paid', '0x...');
-- [DELETED] INSERT INTO `orders` (`id`, `user_id`, `amount`, `status`, `created_at`) VALUES (2, 101, NULL, 'pending', '0x...');
```
