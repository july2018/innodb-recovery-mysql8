#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
InnoDB Recovery Tool for MySQL 8.0
=====================================
基于 MySQL 8.0 InnoDB 源码（rem/rec.h, page0types.h, fil0types.h 等）
模仿 undrop-for-innodb 的思路，实现对 MySQL 8.0 .ibd 文件的数据恢复。

支持功能：
  1. 扫描 .ibd 文件中所有 INDEX 页（包括已删除/被覆盖页的残留记录）
  2. 解析 COMPACT / DYNAMIC（new-style）和 REDUNDANT（old-style）行格式
  3. 根据用户提供的表结构定义解析各字段值
  4. 导出为 SQL INSERT 语句或 CSV 格式
  5. 支持恢复 DELETE-marked（软删除）记录
  6. 支持 MySQL 8.0 新增的 INSTANT ADD COLUMN 标志

MySQL 8.0 关键变化（相比 5.7）：
  - 页类型 FIL_PAGE_SDI（0x0045）用于存储序列化字典信息
  - Instant ADD COLUMN：REC_INFO_INSTANT_FLAG(0x80) / REC_INFO_VERSION_FLAG(0x40)
  - 行版本号字段（instant version byte）在 extra 字节之后、数据之前
  - SDI 页不含用户数据，需跳过
  - checksum 算法默认 crc32（innodb_checksum_algorithm=crc32）

参考源码：
  storage/innobase/rem/rec.h
  storage/innobase/include/page0types.h
  storage/innobase/include/fil0types.h
  storage/innobase/include/fsp0types.h

Author: InnoDB Recovery Tool
Usage:
  python innodb_recovery.py --ibd <file.ibd> --schema <schema.json> [options]
"""

import struct
import sys
import os
import glob
import json
import argparse
import datetime
import binascii
import logging
import subprocess
from typing import Optional, List, Dict, Tuple, Any, Iterator

# ─────────────────────────────────────────────────────────────────
# 常量：来自 MySQL 8.0 源码
# ─────────────────────────────────────────────────────────────────

# FIL 页头偏移（fil0types.h）
FIL_PAGE_SPACE_OR_CHKSUM = 0    # 4 bytes: checksum
FIL_PAGE_OFFSET           = 4    # 4 bytes: page number
FIL_PAGE_PREV             = 8    # 4 bytes: prev page
FIL_PAGE_NEXT             = 12   # 4 bytes: next page
FIL_PAGE_LSN              = 16   # 8 bytes: LSN
FIL_PAGE_TYPE             = 24   # 2 bytes: page type
FIL_PAGE_FILE_FLUSH_LSN   = 26   # 8 bytes
FIL_PAGE_ARCH_LOG_NO      = 34   # 4 bytes (space id in newer versions)
FIL_PAGE_DATA             = 38   # 页数据起始偏移

# 页类型（fil0types.h）
FIL_PAGE_INDEX            = 0x45BF  # B-tree node（叶子页/内部节点页）
FIL_PAGE_RTREE            = 0x45BE  # R-tree node
FIL_PAGE_SDI              = 0x0045  # MySQL 8.0 SDI 序列化字典页（不含用户数据）
FIL_PAGE_TYPE_ALLOCATED   = 0       # 刚分配的空白页
FIL_PAGE_UNDO_LOG         = 2
FIL_PAGE_INODE            = 3
FIL_PAGE_IBUF_FREE_LIST   = 4
FIL_PAGE_TYPE_SYS         = 6
FIL_PAGE_TYPE_TRX_SYS     = 7
FIL_PAGE_TYPE_FSP_HDR     = 8
FIL_PAGE_TYPE_XDES        = 9
FIL_PAGE_TYPE_BLOB        = 10
FIL_PAGE_TYPE_ZBLOB       = 11
FIL_PAGE_TYPE_ZBLOB2      = 12

# 页大小（默认 16KB）
UNIV_PAGE_SIZE = 16384

# PAGE 头部（page0types.h，相对于 FIL_PAGE_DATA 的偏移）
PAGE_N_DIR_SLOTS = 0    # 2 bytes: slot 数量
PAGE_HEAP_TOP    = 2    # 2 bytes: 堆顶指针
PAGE_N_HEAP      = 4    # 2 bytes: 堆中记录数（bit15=compact标志）
PAGE_FREE        = 6    # 2 bytes: 空闲链表头
PAGE_GARBAGE     = 8    # 2 bytes: 已删除记录总字节数
PAGE_LAST_INSERT = 10   # 2 bytes: 最后插入位置
PAGE_DIRECTION   = 12   # 2 bytes
PAGE_N_DIRECTION = 14   # 2 bytes
PAGE_N_RECS      = 16   # 2 bytes: 用户记录数
PAGE_MAX_TRX_ID  = 18   # 8 bytes
PAGE_LEVEL       = 26   # 2 bytes: B-tree 层级（0=叶子）
PAGE_INDEX_ID    = 28   # 8 bytes: 索引 ID

PAGE_HEADER_SIZE = 56   # PAGE 头部字节数（到 BTR_SEG_LEAF 之前）

# Infimum/Supremum 的 extra+data 固定位置（compact 格式）
PAGE_NEW_INFIMUM  = FIL_PAGE_DATA + PAGE_HEADER_SIZE + 20 + 5  # 99
PAGE_NEW_SUPREMUM = FIL_PAGE_DATA + PAGE_HEADER_SIZE + 20 + 5 + 8 + 5  # 112

# 记录头（rem/rec.h）
REC_NEXT              = 2    # 2 bytes: next offset（相对当前记录原点的有符号偏移）
REC_NEW_STATUS        = 3    # 1 byte (低3位): 记录状态
REC_NEW_STATUS_MASK   = 0x07
REC_OLD_SHORT         = 3    # REDUNDANT 短偏移标志（bit0）
REC_OLD_SHORT_MASK    = 0x01
REC_OLD_N_FIELDS      = 4    # REDUNDANT 字段数
REC_OLD_N_FIELDS_MASK = 0x7FE
REC_OLD_N_FIELDS_SHIFT = 1
REC_NEW_HEAP_NO       = 4    # 2 bytes (高13位): heap_no
REC_OLD_HEAP_NO       = 5
REC_HEAP_NO_MASK      = 0xFFF8
REC_HEAP_NO_SHIFT     = 3
REC_NEW_N_OWNED       = 5    # 1 byte (低4位)
REC_OLD_N_OWNED       = 6
REC_N_OWNED_MASK      = 0x0F
REC_NEW_INFO_BITS     = 5    # 1 byte (高4位)
REC_OLD_INFO_BITS     = 6
REC_INFO_BITS_MASK    = 0xF0
REC_INFO_BITS_SHIFT   = 0

# Info bits 标志（MySQL 8.0）
REC_INFO_MIN_REC_FLAG  = 0x10  # 最小记录（非叶子页左边界）
REC_INFO_DELETED_FLAG  = 0x20  # 软删除标志
REC_INFO_VERSION_FLAG  = 0x40  # 行版本号（instant version，MySQL 8.0.29+）
REC_INFO_INSTANT_FLAG  = 0x80  # Instant ADD COLUMN

# 记录状态值（new-style）
REC_STATUS_ORDINARY   = 0
REC_STATUS_NODE_PTR   = 1
REC_STATUS_INFIMUM    = 2
REC_STATUS_SUPREMUM   = 3

# extra bytes 长度
REC_N_OLD_EXTRA_BYTES = 6  # REDUNDANT 格式
REC_N_NEW_EXTRA_BYTES = 5  # COMPACT/DYNAMIC 格式

# NULL 和外部存储标志（REDUNDANT 偏移数组）
REC_1BYTE_SQL_NULL_MASK  = 0x80
REC_2BYTE_SQL_NULL_MASK  = 0x8000
REC_2BYTE_EXTERN_MASK    = 0x4000

# 系统列固定长度
DATA_TRX_ID_LEN   = 6
DATA_ROLL_PTR_LEN = 7

# ─────────────────────────────────────────────────────────────────
# 数据类型定义（模拟 dict0mem.h 中的 DATA_* 常量）
# ─────────────────────────────────────────────────────────────────
DATA_VARCHAR   = 1
DATA_CHAR      = 2
DATA_FIXBINARY = 3
DATA_BINARY    = 4
DATA_BLOB      = 5
DATA_INT       = 6
DATA_SYS       = 8   # 系统列（trx_id / roll_ptr）
DATA_FLOAT     = 9
DATA_DOUBLE    = 10
DATA_DECIMAL   = 11
DATA_VARMYSQL  = 15
DATA_MYSQL     = 16
DATA_POINT     = 17
DATA_GEOMETRY  = 18
DATA_JSON      = 19
DATA_UNSIGNED  = 256  # flag: unsigned int
DATA_BINARY_TYPE = 512

# MySQL 字段类型名称映射（与 schema.json 对接）
MYSQL_TYPE_MAP = {
    'tinyint':    (DATA_INT, 1),
    'smallint':   (DATA_INT, 2),
    'mediumint':  (DATA_INT, 3),
    'int':        (DATA_INT, 4),
    'integer':    (DATA_INT, 4),
    'bigint':     (DATA_INT, 8),
    'float':      (DATA_FLOAT, 4),
    'double':     (DATA_DOUBLE, 8),
    'decimal':    (DATA_DECIMAL, 0),   # 变长
    'numeric':    (DATA_DECIMAL, 0),
    'date':       (DATA_INT, 3),
    'time':       (DATA_INT, 3),
    'year':       (DATA_INT, 1),
    'datetime':   (DATA_INT, 8),
    'timestamp':  (DATA_INT, 4),
    'char':       (DATA_CHAR, 0),      # 0=从定义取
    'varchar':    (DATA_VARCHAR, 0),
    'binary':     (DATA_FIXBINARY, 0),
    'varbinary':  (DATA_BINARY, 0),
    'tinyblob':   (DATA_BLOB, 0),
    'blob':       (DATA_BLOB, 0),
    'mediumblob': (DATA_BLOB, 0),
    'longblob':   (DATA_BLOB, 0),
    'tinytext':   (DATA_BLOB, 0),
    'text':       (DATA_BLOB, 0),
    'mediumtext': (DATA_BLOB, 0),
    'longtext':   (DATA_BLOB, 0),
    'enum':       (DATA_CHAR, 0),
    'set':        (DATA_CHAR, 0),
    'json':       (DATA_JSON, 0),
    'geometry':   (DATA_GEOMETRY, 0),
    'point':      (DATA_POINT, 25),
}

# ─────────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────────

def read_u8(data: bytes, offset: int) -> int:
    return data[offset]

def read_u16_be(data: bytes, offset: int) -> int:
    return struct.unpack_from('>H', data, offset)[0]

def read_u32_be(data: bytes, offset: int) -> int:
    return struct.unpack_from('>I', data, offset)[0]

def read_u64_be(data: bytes, offset: int) -> int:
    return struct.unpack_from('>Q', data, offset)[0]

def read_i16_be(data: bytes, offset: int) -> int:
    """读有符号 16 位大端整数（用于 REC_NEXT 字段）"""
    v = struct.unpack_from('>H', data, offset)[0]
    if v >= 0x8000:
        v -= 0x10000
    return v

# ─────────────────────────────────────────────────────────────────
# Schema 加载
# ─────────────────────────────────────────────────────────────────

class ColumnDef:
    """列定义"""
    def __init__(self, name: str, type_str: str, length: int = 0,
                 nullable: bool = True, unsigned: bool = False,
                 charset: str = 'utf8mb4'):
        self.name = name
        self.type_str = type_str.lower().split('(')[0].strip()
        self.length = length        # 用于 char/varchar/binary/varbinary
        self.nullable = nullable
        self.unsigned = unsigned
        self.charset = charset.lower()

        base = MYSQL_TYPE_MAP.get(self.type_str, (DATA_BLOB, 0))
        self.data_type = base[0]
        # 固定长度：0 表示需要从 length 字段或变长前缀获取
        self.fixed_len = base[1] if base[1] > 0 else 0

        # 对于定长类型，从 length 补充
        if self.data_type == DATA_CHAR and length > 0:
            self.fixed_len = length
        elif self.data_type == DATA_FIXBINARY and length > 0:
            self.fixed_len = length
        elif self.data_type == DATA_INT and self.fixed_len == 0:
            self.fixed_len = 4

        # 时间类型映射
        if self.type_str in ('date',):
            self.fixed_len = 3
        elif self.type_str in ('time',):
            self.fixed_len = 3
        elif self.type_str in ('datetime',):
            self.fixed_len = 8   # MySQL 5.6+ 使用 8 字节或 5+frac
        elif self.type_str in ('timestamp',):
            self.fixed_len = 4
        elif self.type_str == 'year':
            self.fixed_len = 1

        # varchar/varbinary 最大字节数
        self.max_len = length

    def is_variable(self) -> bool:
        return self.data_type in (
            DATA_VARCHAR, DATA_BINARY, DATA_BLOB,
            DATA_VARMYSQL, DATA_JSON, DATA_GEOMETRY
        )

    def is_fixed(self) -> bool:
        return self.fixed_len > 0 and not self.is_variable()

    def __repr__(self):
        return f"<Col {self.name} {self.type_str}({self.length}) fixed={self.fixed_len} var={self.is_variable()}>"


def load_schema(schema_path: str) -> List[ColumnDef]:
    """
    从 JSON 文件加载表结构。
    格式示例：
    {
      "table": "orders",
      "row_format": "DYNAMIC",
      "columns": [
        {"name": "id",    "type": "bigint",       "nullable": false, "unsigned": true},
        {"name": "name",  "type": "varchar(255)",  "nullable": true, "charset": "utf8mb4"},
        {"name": "price", "type": "decimal(10,2)", "nullable": true},
        {"name": "created_at", "type": "datetime", "nullable": false}
      ]
    }
    """
    with open(schema_path, 'r', encoding='utf-8') as f:
        obj = json.load(f)

    cols = []
    for c in obj.get('columns', []):
        type_full = c['type']
        # 解析 varchar(255) 中的长度
        length = 0
        if '(' in type_full:
            try:
                length_str = type_full.split('(')[1].rstrip(')')
                # decimal(10,2) 取总位数 * 最大字节估算
                if ',' in length_str:
                    prec, scale = length_str.split(',')
                    # BCD 编码：ceil((prec-scale)/2) + ceil(scale/2) + 1
                    prec_i = int(prec.strip())
                    length = (prec_i // 2) + 1 + 4
                else:
                    length = int(length_str.strip())
            except Exception:
                length = 0

        nullable = c.get('nullable', True)
        unsigned = c.get('unsigned', False)
        charset = c.get('charset', 'utf8mb4')
        col = ColumnDef(c['name'], type_full, length, nullable, unsigned, charset)
        cols.append(col)

    row_format = obj.get('row_format', 'DYNAMIC').upper()
    table_name = obj.get('table', 'recovered_table')
    return table_name, row_format, cols


# ─────────────────────────────────────────────────────────────────
# InnoDB 页解析
# ─────────────────────────────────────────────────────────────────

class InnoDBPage:
    """代表一个 16KB 的 InnoDB 页"""

    def __init__(self, data: bytes, page_no: int):
        assert len(data) == UNIV_PAGE_SIZE, f"页大小不对: {len(data)}"
        self.data = data
        self.page_no = page_no

        # FIL 头
        self.checksum      = read_u32_be(data, FIL_PAGE_SPACE_OR_CHKSUM)
        self.fil_page_no   = read_u32_be(data, FIL_PAGE_OFFSET)
        self.prev_page     = read_u32_be(data, FIL_PAGE_PREV)
        self.next_page     = read_u32_be(data, FIL_PAGE_NEXT)
        self.lsn           = read_u64_be(data, FIL_PAGE_LSN)
        self.page_type     = read_u16_be(data, FIL_PAGE_TYPE)
        self.space_id      = read_u32_be(data, 34)  # FIL_PAGE_ARCH_LOG_NO / space_id

        # PAGE 头（仅 INDEX 页有效）
        ph_base = FIL_PAGE_DATA
        self.n_dir_slots = read_u16_be(data, ph_base + PAGE_N_DIR_SLOTS)
        self.heap_top    = read_u16_be(data, ph_base + PAGE_HEAP_TOP)
        n_heap_raw       = read_u16_be(data, ph_base + PAGE_N_HEAP)
        self.is_compact  = bool(n_heap_raw & 0x8000)  # bit15 = compact format
        self.n_heap      = n_heap_raw & 0x7FFF
        self.free_offset = read_u16_be(data, ph_base + PAGE_FREE)
        self.page_level  = read_u16_be(data, ph_base + PAGE_LEVEL)
        self.index_id    = read_u64_be(data, ph_base + PAGE_INDEX_ID)
        self.n_recs      = read_u16_be(data, ph_base + PAGE_N_RECS)

    def is_index_page(self) -> bool:
        return self.page_type == FIL_PAGE_INDEX

    def is_leaf(self) -> bool:
        return self.page_level == 0

    def __repr__(self):
        return (f"<Page {self.page_no} type=0x{self.page_type:04x} "
                f"level={self.page_level} idx={self.index_id} "
                f"compact={self.is_compact} n_recs={self.n_recs}>")


# ─────────────────────────────────────────────────────────────────
# 记录解析
# ─────────────────────────────────────────────────────────────────

class RecordParser:
    """
    解析 InnoDB 记录（COMPACT/DYNAMIC new-style 和 REDUNDANT old-style）。
    """

    def __init__(self, page: InnoDBPage, columns: List[ColumnDef],
                 include_deleted: bool = True):
        self.page = page
        self.data = page.data
        self.columns = columns
        self.include_deleted = include_deleted
        # MySQL 8.0 中聚簇索引叶子页总有 trx_id + roll_ptr 系统列
        self.has_sys_cols = True

    # ── New-style（COMPACT / DYNAMIC）记录解析 ──────────────────────

    def _parse_compact_record(self, rec_offset: int) -> Optional[Dict]:
        """
        解析 new-style 记录（COMPACT/DYNAMIC）。

        记录结构（逆序存放在 data 之前）：
          [变长字段长度列表（逆序）]
          [NULL 位图（逆序）]
          [5 字节固定 extra]
            byte -5: info bits (高4) | n_owned (低4)
            byte -4,-3: heap_no (高13 位) | status (低 3 位)
            byte -2,-1: next record offset（有符号 16 位）
          [字段数据...]

        MySQL 8.0 新增：若 REC_INFO_INSTANT_FLAG 置位，则数据区第一个字节
        是 instant version（或 n_fields 前缀），需要特别处理。
        """
        data = self.data
        cols = self.columns

        # ── 读取 extra 5 字节（在 rec_offset 之前） ──
        # extra[-5]: info_bits | n_owned
        info_byte = data[rec_offset - 5]
        info_bits = (info_byte & 0xF0)
        n_owned   = (info_byte & 0x0F)

        # extra[-4,-3]: heap_no | status
        heap_status_16 = read_u16_be(data, rec_offset - 4)
        heap_no = (heap_status_16 & REC_HEAP_NO_MASK) >> REC_HEAP_NO_SHIFT
        status  = heap_status_16 & REC_NEW_STATUS_MASK

        # extra[-2,-1]: next offset
        next_offs_raw = read_u16_be(data, rec_offset - 2)
        # 有符号
        next_offs = next_offs_raw if next_offs_raw < 0x8000 else next_offs_raw - 0x10000

        is_deleted  = bool(info_bits & REC_INFO_DELETED_FLAG)
        is_min_rec  = bool(info_bits & REC_INFO_MIN_REC_FLAG)
        is_instant  = bool(info_bits & REC_INFO_INSTANT_FLAG)
        is_versioned = bool(info_bits & REC_INFO_VERSION_FLAG)

        # 跳过 infimum/supremum
        if status in (REC_STATUS_INFIMUM, REC_STATUS_SUPREMUM):
            return None
        # 跳过内节点指针（非叶子）
        if status == REC_STATUS_NODE_PTR:
            return None

        if is_deleted and not self.include_deleted:
            return None

        # ── 解析 NULL 位图（在 extra 之前，逆序） ──
        # NULL 位图长度 = ceil(nullable_cols / 8)
        nullable_cols = [c for c in cols if c.nullable]
        n_nullable = len(nullable_cols)
        null_bitmap_len = (n_nullable + 7) // 8

        # 变长字段列表在 NULL 位图之前（逆序），每个字段长度 1 或 2 字节
        # 先扫描变长列
        var_cols = [c for c in cols if c.is_variable()]

        # 从 rec_offset - REC_N_NEW_EXTRA_BYTES 往前读 null bitmap
        pos = rec_offset - REC_N_NEW_EXTRA_BYTES  # 5 字节 extra 之前
        null_bitmap = bytearray(null_bitmap_len)
        for i in range(null_bitmap_len):
            pos -= 1
            null_bitmap[i] = data[pos]
        # null_bitmap[0] 对应第 1 个 nullable 列（bit0），逆序存放

        # 再往前是变长字段长度列表（逆序，按字段在表中的顺序倒排）
        var_lengths = {}
        for c in reversed(var_cols):
            if pos <= 0:
                break
            pos -= 1
            vlen = data[pos]
            if vlen & 0x80:
                # 2 字节
                pos -= 1
                vlen = ((vlen & 0x3F) << 8) | data[pos]
            var_lengths[c.name] = vlen

        # ── MySQL 8.0: instant version / n_fields ──
        data_pos = rec_offset
        instant_ver = None
        if is_instant or is_versioned:
            # 第一个字节是 n_fields（1 或 2 字节）或 version byte
            b = data[data_pos]
            if b & 0x80:
                # 2 字节 n_fields
                instant_ver = ((b & 0x7F) << 8) | data[data_pos + 1]
                data_pos += 2
            else:
                instant_ver = b
                data_pos += 1

        # ── 系统列：trx_id (6 bytes) + roll_ptr (7 bytes) ──
        if self.has_sys_cols:
            trx_id = int.from_bytes(data[data_pos:data_pos+6], 'big')
            data_pos += 6
            roll_ptr_raw = data[data_pos:data_pos+7]
            data_pos += 7
        else:
            trx_id = 0
            roll_ptr_raw = b'\x00'*7

        # ── 逐列读取数据 ──
        row = {}
        null_col_idx = 0  # 在 nullable_cols 中的索引
        for col in cols:
            is_null = False
            if col.nullable:
                # null_bitmap 逆序：bit 0 of null_bitmap[0] = 第1个 nullable 列
                bit_pos = null_col_idx
                byte_idx = bit_pos // 8
                bit_idx  = bit_pos % 8
                is_null  = bool(null_bitmap[byte_idx] & (1 << bit_idx))
                null_col_idx += 1

            if is_null:
                row[col.name] = None
                continue

            if col.is_variable():
                vlen = var_lengths.get(col.name, 0)
                val_bytes = data[data_pos:data_pos + vlen]
                data_pos += vlen
                row[col.name] = self._decode_value(col, val_bytes)
            else:
                flen = col.fixed_len
                if flen == 0:
                    # fallback: 跳过
                    row[col.name] = None
                    continue
                val_bytes = data[data_pos:data_pos + flen]
                data_pos += flen
                row[col.name] = self._decode_value(col, val_bytes)

        return {
            'row': row,
            'deleted': is_deleted,
            'heap_no': heap_no,
            'trx_id': trx_id,
            'instant_ver': instant_ver,
            'rec_offset': rec_offset,
        }

    # ── Old-style（REDUNDANT）记录解析 ──────────────────────────────

    def _parse_redundant_record(self, rec_offset: int) -> Optional[Dict]:
        """
        解析 REDUNDANT 格式记录。

        extra 6 字节结构（在 rec_offset 之前）：
          byte -6: info_bits (高4) | n_owned (低4)
          byte -5,-4: heap_no (高13) | short_flag (bit0)
                      & n_fields (bits 1..9) ← merged with heap_no field
          byte -3,-2: [heap_no 高位，参见 REC_OLD_HEAP_NO offset=5]
          byte -2: n_owned
          byte -1: info_bits
        实际参考 rem/rec.h：
          offset 6 (from origin backward): info_bits | n_owned
          offset 5: heap_no (2 bytes)
          offset 3: short_flag | n_fields (2 bytes)
          offset 2 (REC_NEXT): next offset 2 bytes
        """
        data = self.data
        cols = self.columns

        info_byte  = data[rec_offset - 6]   # REC_OLD_INFO_BITS offset=6
        info_bits  = info_byte & 0xF0
        n_owned    = info_byte & 0x0F

        heap_no_raw = read_u16_be(data, rec_offset - 5)  # REC_OLD_HEAP_NO
        heap_no = (heap_no_raw & REC_HEAP_NO_MASK) >> REC_HEAP_NO_SHIFT

        fields_short_raw = read_u16_be(data, rec_offset - 4) # REC_OLD_N_FIELDS / SHORT
        short_flag = fields_short_raw & REC_OLD_SHORT_MASK
        n_fields   = (fields_short_raw & REC_OLD_N_FIELDS_MASK) >> REC_OLD_N_FIELDS_SHIFT

        is_deleted = bool(info_bits & REC_INFO_DELETED_FLAG)
        if is_deleted and not self.include_deleted:
            return None

        # REDUNDANT 偏移数组在 extra 6 字节之前，n_fields 个偏移
        # 每个偏移 1 或 2 字节（由 short_flag 决定）
        offs_size = 1 if short_flag else 2
        offsets = []
        base = rec_offset - REC_N_OLD_EXTRA_BYTES - offs_size * n_fields
        for i in range(n_fields):
            p = base + i * offs_size
            if offs_size == 1:
                o = data[p]
                is_null_flag  = bool(o & REC_1BYTE_SQL_NULL_MASK)
                offset_val    = o & ~REC_1BYTE_SQL_NULL_MASK
            else:
                o = read_u16_be(data, p)
                is_null_flag  = bool(o & REC_2BYTE_SQL_NULL_MASK)
                is_extern     = bool(o & REC_2BYTE_EXTERN_MASK)
                offset_val    = o & ~(REC_2BYTE_SQL_NULL_MASK | REC_2BYTE_EXTERN_MASK)
            offsets.append((offset_val, is_null_flag))

        # 读取系统列
        data_pos = rec_offset
        # REDUNDANT 中，每列的绝对偏移已经在 offsets 里了
        # offsets[i] 是第 i+1 列末尾（相对于 rec origin）的偏移
        row = {}
        prev_end = 0
        sys_col_count = 0  # 跳过 trx_id / roll_ptr（前两个隐藏列）
        trx_id = 0

        for idx, col in enumerate(cols):
            real_idx = idx  # 不含系统列
            if real_idx >= len(offsets):
                break
            end_off, is_null = offsets[real_idx]
            if real_idx > 0:
                start_off = offsets[real_idx - 1][0]
            else:
                start_off = 0

            if is_null:
                row[col.name] = None
            else:
                val_bytes = data[rec_offset + start_off: rec_offset + end_off]
                row[col.name] = self._decode_value(col, val_bytes)

        return {
            'row': row,
            'deleted': is_deleted,
            'heap_no': heap_no,
            'trx_id': trx_id,
            'instant_ver': None,
            'rec_offset': rec_offset,
        }

    # ── 值解码 ──────────────────────────────────────────────────────

    def _decode_value(self, col: ColumnDef, raw: bytes) -> Any:
        """将原始字节解码为 Python 值"""
        if len(raw) == 0:
            return ''

        t = col.data_type
        n = len(raw)

        try:
            if t == DATA_INT:
                # MySQL 整数存储：大端，有符号整数最高字节的最高位翻转
                # 解码：先翻转最高位，再按有符号读取
                b = bytearray(raw)
                b[0] ^= 0x80        # 翻转符号位（有符号和无符号均需此步）
                val = int.from_bytes(b, 'big', signed=False)
                if not col.unsigned:
                    # 转为有符号
                    max_u = (1 << (n * 8))
                    half  = max_u >> 1
                    if val >= half:
                        val -= max_u
                return val

            elif t == DATA_FLOAT:
                # 4 字节浮点，需 XOR 最高位
                b = bytearray(raw)
                b[0] ^= 0x80
                return struct.unpack('>f', bytes(b))[0]

            elif t == DATA_DOUBLE:
                b = bytearray(raw)
                b[0] ^= 0x80
                return struct.unpack('>d', bytes(b))[0]

            elif t == DATA_DECIMAL:
                # BCD 编码的 DECIMAL，简化处理：十六进制展示
                return '0x' + raw.hex()

            elif t in (DATA_VARCHAR, DATA_VARMYSQL, DATA_BLOB, DATA_JSON):
                # 文本：尝试 UTF-8 解码
                charset = getattr(col, 'charset', 'utf8mb4')
                enc = 'utf-8' if 'utf8' in charset else charset
                try:
                    return raw.decode(enc, errors='replace')
                except Exception:
                    return raw.decode('latin1', errors='replace')

            elif t in (DATA_CHAR, DATA_MYSQL):
                charset = getattr(col, 'charset', 'utf8mb4')
                enc = 'utf-8' if 'utf8' in charset else charset
                try:
                    return raw.decode(enc, errors='replace').rstrip('\x00')
                except Exception:
                    return raw.decode('latin1', errors='replace').rstrip('\x00')

            elif t in (DATA_BINARY, DATA_FIXBINARY):
                return '0x' + raw.hex()

            elif t == DATA_GEOMETRY:
                return '0x' + raw.hex()

            else:
                # 默认：日期时间等 —— 原始十六进制
                return '0x' + raw.hex()

        except Exception as e:
            return f'<decode_error:{e}>'

    # ── 日期时间解码辅助 ────────────────────────────────────────────

    @staticmethod
    def decode_datetime_be(raw: bytes) -> str:
        """MySQL 8 字节 DATETIME 解码（5 字节整数 + 微秒）"""
        # 实际上 MySQL DATETIME 用 8 字节（4+4 或 5+3），简化
        if len(raw) < 5:
            return '0x' + raw.hex()
        v = int.from_bytes(raw[:5], 'big')
        # bit layout: year(14) month(4) day(5) hour(5) min(6) sec(6)
        second = v & 0x3F;   v >>= 6
        minute = v & 0x3F;   v >>= 6
        hour   = v & 0x1F;   v >>= 5
        day    = v & 0x1F;   v >>= 5
        month  = v & 0x0F;   v >>= 4
        year   = v
        try:
            return f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{second:02d}"
        except Exception:
            return '0x' + raw.hex()

    # ── 页扫描入口 ──────────────────────────────────────────────────

    def scan_page(self) -> List[Dict]:
        """扫描一页的所有用户记录（包括 delete-marked）"""
        page = self.page
        data = self.data
        results = []

        if not page.is_index_page():
            return results
        if not page.is_leaf():
            return results  # 只处理叶子页

        is_compact = page.is_compact

        if is_compact:
            # COMPACT/DYNAMIC：从 infimum 的 next 开始链式遍历
            # infimum 固定在 PAGE_NEW_INFIMUM (99)
            infimum_offset = FIL_PAGE_DATA + PAGE_HEADER_SIZE + 20 + 5
            # infimum 的 next 字段（REC_NEXT 偏移 -2）
            next_raw = read_u16_be(data, infimum_offset - 2)
            next_off = next_raw if next_raw < 0x8000 else next_raw - 0x10000
            cur = infimum_offset + next_off

            visited = set()
            while cur > FIL_PAGE_DATA and cur < UNIV_PAGE_SIZE - 8:
                if cur in visited:
                    break
                visited.add(cur)

                try:
                    rec = self._parse_compact_record(cur)
                    if rec is not None:
                        results.append(rec)
                except Exception as e:
                    logging.debug(f"解析 compact 记录失败 offset={cur}: {e}")
                    break

                # 移到下一条
                next_raw = read_u16_be(data, cur - 2)
                next_off = next_raw if next_raw < 0x8000 else next_raw - 0x10000
                if next_off == 0:
                    break
                nxt = cur + next_off
                if nxt == cur:
                    break
                cur = nxt

        else:
            # REDUNDANT：类似链式遍历
            infimum_offset = FIL_PAGE_DATA + PAGE_HEADER_SIZE + 20 + 6
            next_raw = read_u16_be(data, infimum_offset - 2)
            cur = next_raw  # REDUNDANT next 是绝对偏移

            visited = set()
            while cur > FIL_PAGE_DATA and cur < UNIV_PAGE_SIZE - 8:
                if cur in visited:
                    break
                visited.add(cur)

                try:
                    rec = self._parse_redundant_record(cur)
                    if rec is not None:
                        results.append(rec)
                except Exception as e:
                    logging.debug(f"解析 redundant 记录失败 offset={cur}: {e}")
                    break

                next_raw = read_u16_be(data, cur - 2)
                if next_raw == 0 or next_raw == cur:
                    break
                cur = next_raw

        return results

    def scan_page_brute_force(self) -> List[Dict]:
        """
        暴力扫描模式：不依赖页链表，直接扫描整页所有可能是记录头的位置。
        用于 drop/truncate 后页链表被损坏的情况。
        """
        data = self.data
        cols  = self.columns
        is_compact = self.page.is_compact
        results = []

        start = FIL_PAGE_DATA + PAGE_HEADER_SIZE + 38  # 跳过系统记录
        end   = read_u16_be(data, FIL_PAGE_DATA + PAGE_HEAP_TOP)
        if end > UNIV_PAGE_SIZE - 8:
            end = UNIV_PAGE_SIZE - 8

        step = 2  # 按 2 字节对齐扫描
        for offset in range(start, end, step):
            if offset + REC_N_NEW_EXTRA_BYTES >= len(data):
                break
            try:
                if is_compact:
                    rec = self._parse_compact_record(offset)
                else:
                    rec = self._parse_redundant_record(offset)
                if rec is not None:
                    # 基本合法性检查：至少一个字段非 None
                    if any(v is not None for v in rec['row'].values()):
                        results.append(rec)
            except Exception:
                pass

        return results


# ─────────────────────────────────────────────────────────────────
# .ibd 文件扫描器
# ─────────────────────────────────────────────────────────────────

class IBDScanner:
    """扫描 .ibd 文件，遍历所有页"""

    def __init__(self, ibd_path: str, columns: List[ColumnDef],
                 row_format: str = 'DYNAMIC',
                 include_deleted: bool = True,
                 brute_force: bool = False,
                 target_index_id: int = 0):
        self.ibd_path = ibd_path
        self.columns = columns
        self.row_format = row_format.upper()
        self.include_deleted = include_deleted
        self.brute_force = brute_force
        self.target_index_id = target_index_id
        self.file_size = os.path.getsize(ibd_path)
        self.total_pages = self.file_size // UNIV_PAGE_SIZE

    def scan(self) -> List[Dict]:
        """扫描整个 .ibd 文件，返回所有恢复的行"""
        all_rows = []
        seen_pks = set()  # 简单去重（基于 row 内容哈希）

        logging.info(f"扫描 {self.ibd_path}，共 {self.total_pages} 页 ...")

        with open(self.ibd_path, 'rb') as f:
            for page_no in range(self.total_pages):
                f.seek(page_no * UNIV_PAGE_SIZE)
                raw = f.read(UNIV_PAGE_SIZE)
                if len(raw) < UNIV_PAGE_SIZE:
                    break

                page = InnoDBPage(raw, page_no)

                if not page.is_index_page():
                    continue
                if not page.is_leaf():
                    continue

                # 如果指定了 index_id，只扫描匹配的
                if self.target_index_id and page.index_id != self.target_index_id:
                    continue

                parser = RecordParser(page, self.columns,
                                      include_deleted=self.include_deleted)

                if self.brute_force:
                    rows = parser.scan_page_brute_force()
                else:
                    rows = parser.scan_page()
                    if not rows:
                        # 链表为空，自动尝试暴力扫描
                        rows = parser.scan_page_brute_force()

                for r in rows:
                    key = self._row_key(r['row'])
                    if key not in seen_pks:
                        seen_pks.add(key)
                        r['page_no'] = page_no
                        all_rows.append(r)

        logging.info(f"共恢复 {len(all_rows)} 条记录")
        return all_rows

    @staticmethod
    def _row_key(row: dict) -> str:
        return str(sorted(row.items()))


# ─────────────────────────────────────────────────────────────────
# /proc/fd 抢救器：DROP TABLE 后 MySQL 进程仍持有文件句柄时使用
# ─────────────────────────────────────────────────────────────────

class ProcFdRescuer:
    """
    DROP TABLE 后文件已从目录项删除，但若 mysqld 进程仍运行，
    内核仍保留文件数据（文件引用计数 > 0）。
    通过 /proc/<pid>/fd/<n> → (deleted) 找到句柄，直接读取字节流。
    仅限 Linux。
    """

    @staticmethod
    def find_mysqld_pids() -> List[int]:
        """返回所有 mysqld 进程的 PID 列表"""
        pids = []
        try:
            out = subprocess.check_output(['pgrep', '-x', 'mysqld'],
                                          stderr=subprocess.DEVNULL).decode()
            pids = [int(p) for p in out.split() if p.strip().isdigit()]
        except Exception:
            pass
        if not pids:
            # 备用方案：扫描 /proc
            for entry in os.listdir('/proc'):
                if not entry.isdigit():
                    continue
                try:
                    with open(f'/proc/{entry}/comm') as f:
                        if f.read().strip() in ('mysqld', 'mysqld_safe'):
                            pids.append(int(entry))
                except Exception:
                    pass
        return pids

    @staticmethod
    def find_deleted_ibd(pid: int, table_hint: str = '') -> List[Tuple[str, str]]:
        """
        扫描 /proc/<pid>/fd，找到已删除的 .ibd 文件句柄。
        返回 [(fd_path, original_name), ...]
        """
        results = []
        fd_dir = f'/proc/{pid}/fd'
        try:
            for fd in os.listdir(fd_dir):
                fd_path = f'{fd_dir}/{fd}'
                try:
                    target = os.readlink(fd_path)
                    if ' (deleted)' in target and '.ibd' in target:
                        if table_hint and table_hint.lower() not in target.lower():
                            continue
                        results.append((fd_path, target.replace(' (deleted)', '')))
                except Exception:
                    pass
        except PermissionError:
            logging.error(f"无权限读取 /proc/{pid}/fd，请用 root 运行")
        return results

    @classmethod
    def rescue(cls, output_path: str, table_hint: str = '') -> Optional[str]:
        """
        自动找到被删除的 .ibd 并保存到 output_path。
        成功返回 output_path，失败返回 None。
        """
        pids = cls.find_mysqld_pids()
        if not pids:
            logging.warning("/proc/fd 方案：未找到 mysqld 进程")
            return None

        for pid in pids:
            deleted = cls.find_deleted_ibd(pid, table_hint)
            if not deleted:
                continue

            logging.info(f"mysqld PID={pid} 持有 {len(deleted)} 个已删除 .ibd 句柄")
            for fd_path, orig_name in deleted:
                print(f"  [found] {orig_name}")

            if len(deleted) > 1 and not table_hint:
                print("发现多个已删除 .ibd，请用 --table 指定表名过滤，例如：")
                for _, orig in deleted:
                    print(f"  --table {os.path.basename(orig).replace('.ibd','')}")
                return None

            fd_path, orig_name = deleted[0]
            logging.info(f"从 {fd_path} 读取数据 → {output_path}")
            try:
                size = 0
                with open(fd_path, 'rb') as src, open(output_path, 'wb') as dst:
                    while True:
                        chunk = src.read(4 * 1024 * 1024)  # 4MB 块
                        if not chunk:
                            break
                        dst.write(chunk)
                        size += len(chunk)
                logging.info(f"抢救完成：{size / 1024 / 1024:.1f} MB → {output_path}")
                return output_path
            except PermissionError:
                logging.error("读取 /proc/fd 失败：需要 root 权限")
                return None
            except Exception as e:
                logging.error(f"读取失败：{e}")
                return None

        logging.warning("在 mysqld 进程中未找到已删除的 .ibd 文件句柄")
        return None


# ─────────────────────────────────────────────────────────────────
# 裸块设备扫描器：DROP TABLE 且 mysqld 已重启时使用
# ─────────────────────────────────────────────────────────────────

class RawDeviceScanner:
    """
    直接扫描块设备（/dev/sda1、/dev/vda3 等）或磁盘镜像文件，
    按 UNIV_PAGE_SIZE(16KB) 步长寻找 InnoDB FIL_PAGE_INDEX 页，
    不依赖 .ibd 文件存在。

    这是 undrop-for-innodb 的核心思路：
      文件删除 → 目录项消失 → 磁盘块仍保留 → 按特征扫描
    """

    # InnoDB 页特征：偏移 24-25 为页类型
    PAGE_TYPE_OFFSET = FIL_PAGE_TYPE   # 24

    def __init__(self, device: str, columns: List[ColumnDef],
                 row_format: str = 'DYNAMIC',
                 include_deleted: bool = True,
                 space_id: int = 0,
                 read_chunk_mb: int = 64):
        self.device = device
        self.columns = columns
        self.row_format = row_format.upper()
        self.include_deleted = include_deleted
        self.space_id = space_id           # 0 = 不过滤 space_id
        self.chunk_size = read_chunk_mb * 1024 * 1024

        if not os.path.exists(device):
            raise FileNotFoundError(f"设备/文件不存在: {device}")

    def _iter_pages(self) -> Iterator[Tuple[int, bytes]]:
        """
        按 16KB 步长遍历设备/文件，yield (page_offset_bytes, raw_bytes)。
        使用大块读取减少 I/O 次数。
        """
        page_sz = UNIV_PAGE_SIZE
        buf_pages = self.chunk_size // page_sz

        with open(self.device, 'rb') as f:
            offset = 0
            while True:
                chunk = f.read(buf_pages * page_sz)
                if not chunk:
                    break
                n = len(chunk) // page_sz
                for i in range(n):
                    yield offset + i * page_sz, chunk[i * page_sz:(i + 1) * page_sz]
                offset += n * page_sz

    def _is_innodb_index_page(self, raw: bytes) -> bool:
        """快速判断是否为 InnoDB INDEX 叶子页"""
        if len(raw) < UNIV_PAGE_SIZE:
            return False
        page_type = read_u16_be(raw, FIL_PAGE_TYPE)
        if page_type != FIL_PAGE_INDEX:
            return False
        # 过滤 space_id
        if self.space_id:
            sid = struct.unpack_from('>I', raw, FIL_PAGE_ARCH_LOG_NO)[0]
            if sid != self.space_id:
                return False
        # 必须是叶子页（level == 0）
        level = read_u16_be(raw, FIL_PAGE_DATA + PAGE_LEVEL)
        if level != 0:
            return False
        # N_HEAP 合法性（bit15 = compact flag）
        n_heap = read_u16_be(raw, FIL_PAGE_DATA + PAGE_N_HEAP) & 0x7FFF
        if n_heap < 2 or n_heap > 2000:
            return False
        return True

    def scan(self) -> List[Dict]:
        """
        扫描整个设备，返回所有恢复的行。
        """
        all_rows = []
        seen_keys = set()
        pages_checked = 0
        pages_matched = 0

        logging.info(f"裸盘扫描: {self.device}，行格式: {self.row_format}")
        logging.info("注意：扫描大磁盘可能需要数分钟到数小时，建议指定 --offset / --length 缩小范围")

        for byte_offset, raw in self._iter_pages():
            pages_checked += 1
            if pages_checked % 100000 == 0:
                gb = byte_offset / 1024**3
                logging.info(f"已扫描 {gb:.2f} GB，匹配页: {pages_matched}，"
                             f"已恢复: {len(all_rows)} 条")

            if not self._is_innodb_index_page(raw):
                continue

            pages_matched += 1
            page_no_in_file = byte_offset // UNIV_PAGE_SIZE

            try:
                page = InnoDBPage(raw, page_no_in_file)
                parser = RecordParser(page, self.columns,
                                      include_deleted=self.include_deleted)
                rows = parser.scan_page()
                if not rows:
                    rows = parser.scan_page_brute_force()

                for r in rows:
                    key = IBDScanner._row_key(r['row'])
                    if key not in seen_keys:
                        seen_keys.add(key)
                        r['page_no'] = page_no_in_file
                        r['device_offset'] = byte_offset
                        all_rows.append(r)
            except Exception as e:
                logging.debug(f"解析页 offset={byte_offset} 失败: {e}")

        logging.info(f"裸盘扫描完成：检查 {pages_checked} 页，"
                     f"匹配 {pages_matched} 个 INDEX 页，"
                     f"恢复 {len(all_rows)} 条记录")
        return all_rows

    def scan_range(self, start_byte: int, length_bytes: int) -> List[Dict]:
        """
        只扫描指定字节范围（用于缩小范围、加速）。
        start_byte 和 length_bytes 都需要是 16384 的倍数。
        """
        all_rows = []
        seen_keys = set()
        page_sz = UNIV_PAGE_SIZE
        start_page = start_byte // page_sz
        end_page   = (start_byte + length_bytes) // page_sz

        logging.info(f"范围扫描: offset={start_byte}({start_byte//1024//1024}MB) "
                     f"length={length_bytes//1024//1024}MB")

        with open(self.device, 'rb') as f:
            f.seek(start_byte)
            for pg in range(start_page, end_page):
                raw = f.read(page_sz)
                if len(raw) < page_sz:
                    break
                if not self._is_innodb_index_page(raw):
                    continue
                try:
                    page = InnoDBPage(raw, pg)
                    parser = RecordParser(page, self.columns,
                                         include_deleted=self.include_deleted)
                    rows = parser.scan_page()
                    if not rows:
                        rows = parser.scan_page_brute_force()
                    for r in rows:
                        key = IBDScanner._row_key(r['row'])
                        if key not in seen_keys:
                            seen_keys.add(key)
                            r['page_no'] = pg
                            all_rows.append(r)
                except Exception:
                    pass

        logging.info(f"范围扫描完成，恢复 {len(all_rows)} 条")
        return all_rows


def detect_mysql_datadir() -> str:
    """尝试自动检测 MySQL 数据目录"""
    candidates = [
        '/var/lib/mysql',
        '/usr/local/mysql/data',
        '/data/mysql',
        '/opt/mysql/data',
    ]
    for c in candidates:
        if os.path.isdir(c):
            return c
    # 尝试从运行进程读取
    try:
        out = subprocess.check_output(
            ['mysql', '-e', 'SELECT @@datadir', '-s', '--skip-column-names'],
            stderr=subprocess.DEVNULL
        ).decode().strip()
        if out and os.path.isdir(out):
            return out
    except Exception:
        pass
    return ''


def detect_device_of_path(path: str) -> str:
    """找到 path 所在的块设备（Linux）"""
    try:
        out = subprocess.check_output(['df', '--output=source', path],
                                      stderr=subprocess.DEVNULL).decode()
        lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
        if len(lines) >= 2:
            return lines[1]
    except Exception:
        pass
    return ''


# ─────────────────────────────────────────────────────────────────
# 输出格式化
# ─────────────────────────────────────────────────────────────────

class OutputWriter:

    def __init__(self, table_name: str, columns: List[ColumnDef]):
        self.table_name = table_name
        self.columns = columns

    def _escape_sql(self, val: Any) -> str:
        if val is None:
            return 'NULL'
        if isinstance(val, str):
            v = val.replace("\\", "\\\\").replace("'", "\\'")
            return f"'{v}'"
        if isinstance(val, (int, float)):
            return str(val)
        return f"'{str(val)}'"

    def to_sql(self, rows: List[Dict], include_deleted_comment: bool = True) -> str:
        lines = []
        lines.append(f"-- MySQL 8.0 InnoDB Recovery Tool")
        lines.append(f"-- Table: {self.table_name}")
        lines.append(f"-- Recovered: {datetime.datetime.now().isoformat()}")
        lines.append(f"-- Total rows: {len(rows)}")
        lines.append("")

        col_names = ', '.join(f'`{c.name}`' for c in self.columns)

        for r in rows:
            row = r['row']
            vals = ', '.join(self._escape_sql(row.get(c.name)) for c in self.columns)
            stmt = f"INSERT INTO `{self.table_name}` ({col_names}) VALUES ({vals});"
            if include_deleted_comment and r.get('deleted'):
                stmt = f"-- [DELETED] " + stmt
            lines.append(stmt)

        return '\n'.join(lines)

    def to_csv(self, rows: List[Dict]) -> str:
        import csv
        import io
        buf = io.StringIO()
        col_names = [c.name for c in self.columns] + ['_deleted', '_heap_no', '_page_no', '_trx_id']
        writer = csv.writer(buf)
        writer.writerow(col_names)
        for r in rows:
            row = r['row']
            line = [row.get(c.name) for c in self.columns]
            line += [r.get('deleted', False), r.get('heap_no', 0),
                     r.get('page_no', 0), r.get('trx_id', 0)]
            writer.writerow(line)
        return buf.getvalue()

    def to_json(self, rows: List[Dict]) -> str:
        out = []
        for r in rows:
            row_copy = dict(r['row'])
            row_copy['_meta'] = {
                'deleted': r.get('deleted', False),
                'heap_no': r.get('heap_no', 0),
                'page_no': r.get('page_no', 0),
                'trx_id':  r.get('trx_id', 0),
            }
            out.append(row_copy)
        return json.dumps(out, ensure_ascii=False, indent=2, default=str)


# ─────────────────────────────────────────────────────────────────
# 交互式 Schema 生成辅助
# ─────────────────────────────────────────────────────────────────

def generate_schema_template(table_name: str, output_path: str):
    """生成一个 schema.json 模板供用户填写"""
    tmpl = {
        "table": table_name,
        "row_format": "DYNAMIC",
        "comment": "请根据 CREATE TABLE 语句填写列定义。row_format 可为 COMPACT/DYNAMIC/REDUNDANT",
        "columns": [
            {"name": "id",         "type": "bigint",       "nullable": False, "unsigned": True},
            {"name": "name",       "type": "varchar(255)",  "nullable": True,  "charset": "utf8mb4"},
            {"name": "price",      "type": "decimal(10,2)", "nullable": True},
            {"name": "created_at", "type": "datetime",      "nullable": False}
        ]
    }
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(tmpl, f, ensure_ascii=False, indent=2)
    print(f"Schema 模板已写入: {output_path}")


# ─────────────────────────────────────────────────────────────────
# 主程序
# ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='InnoDB Recovery Tool for MySQL 8.0\n'
                    '支持三种恢复模式：.ibd文件 / /proc/fd抢救 / 裸块设备扫描',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
恢复场景与命令示例：

【场景1】DROP TABLE / DELETE 后 .ibd 仍存在（文件未被覆盖）：
  python innodb_recovery.py --ibd orders.ibd --schema schema.json --brute-force -o out.sql

【场景2】DROP TABLE 后 mysqld 进程仍在运行（/proc/fd 抢救，成功率最高）：
  python innodb_recovery.py --rescue --table orders --schema schema.json -o out.sql
  # 工具自动从 /proc/<mysqld_pid>/fd 读取已删除文件，无需 .ibd 存在

【场景3】mysqld 已重启 / .ibd 已删除，扫描裸块设备（需 root）：
  python innodb_recovery.py --device /dev/sda1 --schema schema.json -o out.sql
  # 扫描大磁盘时可加 --offset / --length 缩小范围（单位 MB）
  python innodb_recovery.py --device /dev/sda1 --schema schema.json \\
      --offset 10240 --length 20480 -o out.sql

【其他】
  # 生成表结构模板
  python innodb_recovery.py --gen-schema orders --schema-out schema.json

  # 查看页分布（.ibd 存在时）
  python innodb_recovery.py --ibd orders.ibd --page-info

  # 自动检测 MySQL 数据目录所在磁盘（辅助信息）
  python innodb_recovery.py --detect-device

注意：裸盘扫描和 /proc/fd 抢救均需 root 权限。
        """
    )

    # ── 数据源（三选一）──
    src_group = parser.add_mutually_exclusive_group()
    src_group.add_argument('--ibd',    help='.ibd 文件路径（或 ibdata1）')
    src_group.add_argument('--device', help='裸块设备路径，如 /dev/sda1（需 root）')
    src_group.add_argument('--rescue', action='store_true',
                           help='从 /proc/fd 抢救被 DROP 的表（mysqld 必须在运行，需 root）')

    # ── 通用参数 ──
    parser.add_argument('--schema',   help='表结构 JSON 文件路径')
    parser.add_argument('--table',    help='表名（用于 --rescue 时过滤，或生成输出文件名）')
    parser.add_argument('-o', '--output', help='输出文件路径（默认 stdout）')
    parser.add_argument('--format',   choices=['sql', 'csv', 'json'],
                        default='sql', help='输出格式（默认 sql）')
    parser.add_argument('--no-deleted', action='store_true',
                        help='不输出 delete-marked 记录')
    parser.add_argument('--brute-force', action='store_true',
                        help='暴力扫描模式（链表被破坏时使用）')
    parser.add_argument('--index-id', type=int, default=0,
                        help='只扫描指定 index_id 的页')

    # ── 裸盘专用参数 ──
    parser.add_argument('--offset',   type=int, default=0,
                        help='裸盘扫描起始位置（MB，默认从头开始）')
    parser.add_argument('--length',   type=int, default=0,
                        help='裸盘扫描长度（MB，默认扫描全部）')
    parser.add_argument('--space-id', type=int, default=0,
                        help='只匹配指定 InnoDB space_id 的页（0=不过滤）')
    parser.add_argument('--read-chunk', type=int, default=64,
                        help='裸盘每次读取块大小（MB，默认64）')

    # ── 辅助命令 ──
    parser.add_argument('--gen-schema', metavar='TABLE_NAME',
                        help='生成 schema 模板')
    parser.add_argument('--schema-out', default='schema_template.json',
                        help='schema 模板输出路径')
    parser.add_argument('--page-info',  action='store_true',
                        help='打印 .ibd 文件页类型统计（需 --ibd）')
    parser.add_argument('--detect-device', action='store_true',
                        help='自动检测 MySQL 数据目录所在块设备')
    parser.add_argument('--system',    action='store_true',
                        help='扫描系统表空间（ibdata1）')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='详细日志')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s'
    )

    # ── 辅助：生成 schema 模板 ──
    if args.gen_schema:
        generate_schema_template(args.gen_schema, args.schema_out)
        return

    # ── 辅助：检测设备 ──
    if args.detect_device:
        datadir = detect_mysql_datadir()
        if datadir:
            dev = detect_device_of_path(datadir)
            print(f"MySQL 数据目录: {datadir}")
            print(f"所在块设备:     {dev if dev else '(无法自动检测，请手动运行 df -h /var/lib/mysql)'}")
        else:
            print("未能自动检测 MySQL 数据目录，请手动运行：df -h /var/lib/mysql")
        return

    # ── 辅助：页信息 ──
    if args.page_info:
        if not args.ibd:
            print("需要 --ibd 参数")
            sys.exit(1)
        page_type_names = {
            FIL_PAGE_INDEX: 'INDEX',
            FIL_PAGE_SDI:   'SDI(8.0字典)',
            FIL_PAGE_TYPE_ALLOCATED: 'ALLOCATED',
            FIL_PAGE_UNDO_LOG: 'UNDO_LOG',
            FIL_PAGE_INODE:  'INODE',
            FIL_PAGE_TYPE_FSP_HDR: 'FSP_HDR',
            FIL_PAGE_TYPE_XDES: 'XDES',
            FIL_PAGE_TYPE_BLOB: 'BLOB',
        }
        counts = {}
        with open(args.ibd, 'rb') as f:
            page_no = 0
            while True:
                raw = f.read(UNIV_PAGE_SIZE)
                if len(raw) < UNIV_PAGE_SIZE:
                    break
                pt = read_u16_be(raw, FIL_PAGE_TYPE)
                name = page_type_names.get(pt, f'0x{pt:04x}')
                counts[name] = counts.get(name, 0) + 1
                page_no += 1
        print(f"\n文件: {args.ibd}  共 {page_no} 页")
        print(f"{'页类型':<25} {'数量':>8}")
        print('-' * 35)
        for k, v in sorted(counts.items(), key=lambda x: -x[1]):
            print(f"{k:<25} {v:>8}")
        return

    # ── 检查 schema ──
    if not args.schema and not args.gen_schema:
        parser.print_help()
        print("\n错误: 需要 --schema 参数（或先用 --gen-schema 生成模板）")
        sys.exit(1)

    table_name, row_format, columns = load_schema(args.schema)
    logging.info(f"表: {table_name}  行格式: {row_format}  列数: {len(columns)}")

    rows = []

    # ════════════════════════════════════════════════════════════
    # 模式 A：/proc/fd 抢救（mysqld 仍在运行）
    # ════════════════════════════════════════════════════════════
    if args.rescue:
        if os.geteuid() != 0:
            logging.warning("建议以 root 运行以访问 /proc/<mysqld_pid>/fd")

        table_hint = args.table or table_name
        rescued_path = f'/tmp/rescued_{table_hint}.ibd'
        result = ProcFdRescuer.rescue(rescued_path, table_hint)

        if result is None:
            print("\n/proc/fd 抢救失败。")
            print("请检查：")
            print("  1. mysqld 是否仍在运行（ps aux | grep mysqld）")
            print("  2. 是否以 root 身份运行本工具")
            print("  3. 若 mysqld 已重启，改用裸盘扫描：--device /dev/sdaX")
            sys.exit(1)

        logging.info(f"使用抢救的文件继续扫描: {rescued_path}")
        scanner = IBDScanner(
            ibd_path=rescued_path,
            columns=columns,
            row_format=row_format,
            include_deleted=not args.no_deleted,
            brute_force=True,   # 抢救的文件强制暴力扫描
            target_index_id=args.index_id,
        )
        rows = scanner.scan()

    # ════════════════════════════════════════════════════════════
    # 模式 B：裸块设备扫描
    # ════════════════════════════════════════════════════════════
    elif args.device:
        if not os.path.exists(args.device):
            print(f"错误: 设备不存在: {args.device}")
            print("提示: 运行 --detect-device 自动检测正确的设备路径")
            sys.exit(1)

        scanner_dev = RawDeviceScanner(
            device=args.device,
            columns=columns,
            row_format=row_format,
            include_deleted=not args.no_deleted,
            space_id=args.space_id,
            read_chunk_mb=args.read_chunk,
        )

        if args.offset or args.length:
            start_bytes  = args.offset * 1024 * 1024
            length_bytes = args.length * 1024 * 1024 if args.length else None
            if length_bytes is None:
                # 扫描到设备末尾
                dev_size = os.path.getsize(args.device)
                length_bytes = dev_size - start_bytes
            rows = scanner_dev.scan_range(start_bytes, length_bytes)
        else:
            rows = scanner_dev.scan()

    # ════════════════════════════════════════════════════════════
    # 模式 C：.ibd 文件扫描（原有逻辑）
    # ════════════════════════════════════════════════════════════
    elif args.ibd:
        if not os.path.exists(args.ibd):
            print(f"错误: 文件不存在: {args.ibd}")
            print()
            print("DROP TABLE 后 .ibd 文件已被删除，有以下恢复方案：")
            print()
            print("  方案1（推荐）- mysqld 仍在运行时，从 /proc/fd 抢救：")
            print(f"    python {sys.argv[0]} --rescue --table {os.path.basename(args.ibd).replace('.ibd','')} --schema {args.schema or 'schema.json'} -o out.sql")
            print()
            print("  方案2 - mysqld 已重启，扫描裸块设备（需 root）：")
            datadir = detect_mysql_datadir()
            dev = detect_device_of_path(datadir) if datadir else '/dev/sda1'
            print(f"    python {sys.argv[0]} --device {dev} --schema {args.schema or 'schema.json'} -o out.sql")
            print()
            print("  如不确定设备，先运行：")
            print(f"    python {sys.argv[0]} --detect-device")
            sys.exit(1)

        scanner = IBDScanner(
            ibd_path=args.ibd,
            columns=columns,
            row_format=row_format,
            include_deleted=not args.no_deleted,
            brute_force=args.brute_force,
            target_index_id=args.index_id,
        )
        rows = scanner.scan()

    else:
        parser.print_help()
        sys.exit(1)

    if not rows:
        logging.warning("未恢复到任何记录。建议：")
        logging.warning("  - 若使用 --ibd，尝试加 --brute-force")
        logging.warning("  - 若使用 --device，尝试指定 --space-id 或 --offset/--length 缩小范围")

    # ── 输出 ──
    writer = OutputWriter(table_name, columns)
    if args.format == 'sql':
        content = writer.to_sql(rows)
    elif args.format == 'csv':
        content = writer.to_csv(rows)
    else:
        content = writer.to_json(rows)

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(content)
        logging.info(f"结果已写入: {args.output}  共 {len(rows)} 条记录")
    else:
        print(content)


if __name__ == '__main__':
    main()
