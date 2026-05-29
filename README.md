# InnoDB Recovery Tool for MySQL 8.0

> 模仿 [undrop-for-innodb](https://github.com/twindb/undrop-for-innodb) 的思路，  
> 结合 MySQL 8.0 InnoDB 源码（`storage/innobase/`），  
> 实现对 MySQL 8.0 `.ibd` 文件的数据恢复。

**核心能力：**
- 🔍 三种恢复模式：`.ibd` 文件 / `/proc/fd` 抢救 / 裸盘扫描
- 🤖 **SDI 自动提取**：从 MySQL 8.0 SDI 页自动获取表结构，无需手写 `schema.json`
- ⚡ 多线程并行扫描（`--workers N`），支持裸盘快速预扫描
- 🧪 完整支持 COMPACT / DYNAMIC / REDUNDANT 行格式 & Instant ADD COLUMN

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
- Linux（裸盘扫描和 /proc/fd 抢救需要 root）

### 快速开始

**🚀 最简单的方式：SDI 自动提取（无需手写 schema.json）**

MySQL 8.0 在 `.ibd` 文件中嵌入了序列化字典信息（SDI）页，工具可自动提取完整表结构：

```bash
# .ibd 文件存在时
python innodb_recovery.py --ibd orders.ibd --auto-schema -o recovered.sql

# 裸盘扫描时自动提取（DROP TABLE 后推荐）
python innodb_recovery.py --device /dev/vda3 --auto-schema --table orders \
    --workers 8 --relaxed -o recovered.sql

# 如果不需要提取表结构，也可以手动创建 schema.json
python innodb_recovery.py --gen-schema your_table --schema-out schema.json
```

**手动 schema.json 格式（若不使用 --auto-schema）：**

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

**Step 2：恢复数据（根据场景选择模式）**

### 场景一：.ibd 文件仍在（未DROP）——最可靠

```bash
# 恢复所有记录（含软删除），输出 SQL
python innodb_recovery.py --ibd orders.ibd --schema schema.json -o recovered.sql

# 仅恢复活跃记录
python innodb_recovery.py --ibd orders.ibd --schema schema.json --no-deleted -o active.sql

# 暴力扫描（DROP/TRUNCATE 后页链表可能断裂）
python innodb_recovery.py --ibd orders.ibd --schema schema.json --brute-force -o recovered.sql

# 输出 CSV
python innodb_recovery.py --ibd orders.ibd --schema schema.json --format csv -o recovered.csv
```

### 场景二：DROP TABLE（.ibd 已删除）—— 裸盘扫描 + SDI 自动提取 ⭐推荐

> **重要：** MySQL 8.0 的 DROP TABLE 会立即关闭 `.ibd` 文件句柄，
> 所以 `/proc/fd` 方案对此场景无效。数据仍在磁盘上，裸盘扫描是正确方式。

```bash
# 第一步：检测设备路径
python innodb_recovery.py --detect-device

# 第二步：快速预扫描定位数据位置（不需要 schema，极快）
python innodb_recovery.py --device /dev/vda3 --quick-scan --workers 8

# 第三步：精准恢复 + 自动提取表结构（推荐）
python innodb_recovery.py --device /dev/vda3 \
    --auto-schema --table orders \
    --offset 18300 --length 1200 \
    --workers 8 --relaxed -o recovered.sql

# 或一步到位：全盘扫描 + 自动提取 schema
python innodb_recovery.py --device /dev/vda3 --auto-schema --table orders \
    --workers 8 --relaxed -o recovered.sql
```

### 场景三：mysqld 已重启，.ibd 完全删除 —— 裸盘扫描

**推荐两步法：先预扫描定位，再精准恢复。**

```bash
# 第1步：快速预扫描（极快，只读页类型，不需要 schema）
python innodb_recovery.py --device /dev/vda3 --quick-scan --workers 8

# 输出示例：
#   找到 3,241 个候选 InnoDB 页。
#   候选页位置分布:
#     18-19 GB: INDEX:3241
#   使用以下命令进行恢复：
#     python innodb_recovery.py --device /dev/vda3 \
#         --schema schema.json \
#         --offset 18300 --length 1200 \
#         --workers 8 --relaxed -o recovered.sql

# 第2步：精准恢复（按预扫描建议的范围）
python innodb_recovery.py --device /dev/vda3 \
    --schema schema.json \
    --offset 18300 --length 1200 \
    --workers 8 --relaxed -o recovered.sql

# 如果不确定范围，也可以全盘扫描（耗时但完整）
python innodb_recovery.py --device /dev/vda3 \
    --schema schema.json \
    --workers 8 --relaxed -o recovered.sql
```

**辅助：自动检测数据目录所在设备**

```bash
python innodb_recovery.py --detect-device
# 输出: MySQL 数据目录: /data/mysql
#       所在块设备:     /dev/vda3
```

---
## 恢复模式对比

| 模式 | 速度 | 成功率 | 使用条件 |
|------|------|--------|----------|
| `--ibd` | ⭐⭐⭐ 最快 | ⭐⭐⭐ 最高 | .ibd 文件仍存在 |
| `--device --auto-schema` | ⭐⭐ 中 | ⭐⭐⭐ 高 | DROP TABLE 后，root |
| `--device --schema` | ⭐⭐ 中 | ⭐⭐ 中 | 手动提供 schema，root |
| `--rescue` | ⭐⭐⭐ 快 | ⭐ 低 | 仅误 `rm` .ibd 文件场景 |

---

## 参数说明

| 参数 | 说明 |
|------|------|
| **数据源（三选一）** | |
| `--ibd FILE` | `.ibd` 文件路径（MySQL 8.0 每表一个文件） |
| `--device DEV` | 裸块设备路径，如 `/dev/vda3`（需 root） |
| `--rescue` | 从 `/proc/fd` 抢救被 DROP 的表（mysqld 必须在运行，需 root） |
| **通用参数** | |
| `--schema FILE` | 表结构 JSON 文件 |
| `--auto-schema` | 自动从 MySQL 8.0 SDI 页提取表结构（推荐，无需手写 schema） |
| `--table NAME` | 表名（用于过滤或输出文件名） |
| `-o FILE` | 输出路径（不指定则 stdout） |
| `--format sql\|csv\|json` | 输出格式，默认 sql |
| `--no-deleted` | 排除 delete-marked 记录 |
| `--brute-force` | 暴力扫描模式，适用于链表损坏 |
| `--index-id N` | 只扫描指定 index_id（可从 --page-info 获取）|
| **裸盘专用参数** | |
| `--offset N` | 扫描起始位置（MB），默认 0 |
| `--length N` | 扫描长度（MB），默认全部 |
| `--workers N` | 并行线程数（默认 4） |
| `--relaxed` | 宽松检测：接受所有 INDEX/BLOB 页，不限 level |
| `--quick-scan` | 快速预扫描：只定位页位置，不做恢复（给出 `--offset` 建议） |
| `--space-id N` | 只匹配指定 InnoDB space_id |
| `--read-chunk N` | 每次读取块大小（MB，默认 64） |
| **辅助命令** | |
| `--page-info` | 打印页类型统计，不执行恢复 |
| `--gen-schema TABLE` | 生成 schema 模板 |
| `--schema-out FILE` | schema 模板输出路径 |
| `--detect-device` | 自动检测 MySQL 数据目录所在设备 |
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

### 场景二：DROP TABLE（.ibd 已删除）—— 裸盘扫描

> **注意：** MySQL DROP TABLE 会立即关闭 .ibd 文件句柄，所以 `/proc/fd` 通常找不到数据。  
> 但数据仍在磁盘上，裸盘扫描是正确方式。

```bash
# 第一步：检测数据目录所在设备
python innodb_recovery.py --detect-device

# 第二步：快速预扫描定位数据（不需要 schema，极快）
python innodb_recovery.py --device /dev/vda3 --quick-scan --workers 8

# 第三步：按预扫描给出的范围，精准恢复 + 自动提取 schema
python innodb_recovery.py --device /dev/vda3 \
    --auto-schema --table orders \
    --offset 18300 --length 1200 \
    --workers 8 --relaxed -o recovered.sql

# 或者直接一步到位（全盘扫描，耗时但完整）
python innodb_recovery.py --device /dev/vda3 --auto-schema --table orders \
    --workers 8 --relaxed -o recovered.sql
```

### 场景三：DELETE 误删（.ibd 仍在）

```bash
# 第一步：检测数据目录所在设备
python innodb_recovery.py --detect-device

# 第二步：快速预扫描定位数据（不需要 schema）
python innodb_recovery.py --device /dev/vda3 --quick-scan --workers 8

# 第三步：按预扫描给出的范围精准恢复
python innodb_recovery.py --device /dev/vda3 \
    --schema schema.json \
    --offset 18300 --length 1200 \
    --workers 8 --relaxed -o recovered.sql
```

### 场景四：TRUNCATE TABLE（.ibd 仍在）

```bash
# TRUNCATE 创建新 .ibd 文件，旧数据丢失。同 DROP TABLE 场景三。
# 如果 .ibd 仍存在，暴力扫描
python innodb_recovery.py --ibd orders.ibd --schema schema.json --brute-force -o recovered.sql
```

### 场景五：从 ibdata1 恢复（共享表空间）

```bash
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

## 常见问题

### Q: 裸盘扫描 1.5GB 后报 0 匹配页？

**A:** 这是正常的。文件系统在一开始存放的是超级块、inode表等元数据，MySQL的数据位于磁盘深处（可能 10-50GB 之后）。

**解决：先用 `--quick-scan` 预扫描定位数据位置。**

```bash
# 全盘快速扫描，不进深度恢复，找到数据位置
python innodb_recovery.py --device /dev/vda3 --quick-scan --workers 8
# 按输出建议的 --offset/--length 执行精准恢复
```

### Q: 预扫描找到候选页但没有恢复出数据？

- 加上 `--relaxed` 放宽检测条件（接受非叶子页、BLOB页等）
- 尝试 `--brute-force` 暴力模式
- 检查 schema 定义是否与实际表结构完全一致（列顺序！）

### Q: 扫描速度太慢？

- **预扫描**：约 500-3000 MB/s（取决于磁盘）— 40GB 约 13-80 秒
- **深度恢复**：增加 `--workers`（建议 8-16 线程）可 4-8 倍加速
- 使用 `--offset + --length` 缩小扫描范围（预扫描会给出建议范围）

### Q: /proc/fd 抢救失败（"未找到已删除的 .ibd 文件句柄"）？

**A:** 这是正常的。MySQL 8.0 的 `DROP TABLE` 会调用 `fil_delete_tablespace()` 主动关闭 `.ibd` 文件句柄，所以 `/proc/<pid>/fd` 中找不到。

`/proc/fd` 方案仅适用于**误 `rm` .ibd 文件**（而非 DROP TABLE）的场景。

对于 DROP TABLE，请使用裸盘扫描 + SDI 自动提取：
```bash
python innodb_recovery.py --device /dev/vda3 --auto-schema --table your_table \
    --workers 8 --relaxed -o recovered.sql
```

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
