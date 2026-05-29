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
import time
import hashlib
import zlib
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    DROP TABLE 后抢救数据。

    策略层次（按优先级）：
      1. /proc/<pid>/fd     — 已删除但未关闭的文件句柄（DROP TABLE 后通常已关闭）
      2. /proc/<pid>/map_files — 内存映射的已删除文件（mmap 区域）
      3. 裸盘扫描回退           — 以上均无效时建议

    注意：MySQL 8.0 DROP TABLE 会调用 fil_delete_tablespace() 关闭句柄，
    所以策略1通常无效。策略2（map_files）在以下情况有效：
      - InnoDB buffer pool 使用 O_DIRECT 但未完全释放
      - 其他进程持有该文件的 mmap

    仅在 Linux 上可用。
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

        注意：DROP TABLE 后 MySQL 通常已关闭 .ibd 句柄，
        此方法主要用于误 rm .ibd 文件的场景。
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
        except FileNotFoundError:
            pass
        return results

    @staticmethod
    def find_mapped_deleted_ibd(pid: int, table_hint: str = '') -> List[Tuple[str, str]]:
        """
        扫描 /proc/<pid>/map_files，找到内存映射的已删除 .ibd 文件。

        当 InnoDB 使用 O_DIRECT 打开 .ibd 文件后 DROP TABLE，
        文件可能已经从目录项删除，但内核的 page cache 和 mmap
        映射可能仍存在（取决于内核版本和配置）。

        map_files 目录包含进程地址空间中每个映射区域的信息。
        每个条目是一个符号链接，指向映射的文件路径。
        """
        results = []
        map_dir = f'/proc/{pid}/map_files'
        try:
            entries = os.listdir(map_dir)
        except (FileNotFoundError, PermissionError) as e:
            logging.debug(f"无法访问 {map_dir}: {e}")
            return results

        for entry in entries:
            try:
                target = os.readlink(f'{map_dir}/{entry}')
                # map_files 中的已删除文件也可能带 (deleted) 后缀
                if ' (deleted)' in target and '.ibd' in target:
                    orig = target.replace(' (deleted)', '')
                    if table_hint and table_hint.lower() not in orig.lower():
                        continue
                    results.append((f'{map_dir}/{entry}', orig))
            except Exception:
                pass

        return results

    @staticmethod
    def _try_read_mapped_file(map_entry: str) -> Optional[bytes]:
        """
        尝试通过 /proc/<pid>/mem 读取内存映射的文件数据。

        从 map_files 条目（如 "7f1234000000-7f1235000000"）解析地址范围，
        然后通过 /proc/<pid>/mem seek 到对应位置读取数据。

        返回文件内容 bytes，失败返回 None。
        """
        parts = map_entry.split('/')[-1]  # "7f1234000000-7f1235000000"
        try:
            start_str, end_str = parts.split('-')
            start = int(start_str, 16)
            end = int(end_str, 16)
            size = end - start
            if size < UNIV_PAGE_SIZE or size > 10 * 1024 * 1024 * 1024:
                return None
        except (ValueError, AttributeError):
            return None

        pid = int(map_entry.split('/')[2])  # /proc/<pid>/map_files/...
        mem_path = f'/proc/{pid}/mem'

        try:
            with open(mem_path, 'rb') as f:
                # 只读前 1GB（避免超时）
                read_size = min(size, 1024 * 1024 * 1024)
                f.seek(start)
                return f.read(read_size)
        except Exception as e:
            logging.debug(f"读取 /proc/{pid}/mem 失败: {e}")
            return None

    @classmethod
    def rescue(cls, output_path: str, table_hint: str = '') -> Optional[str]:
        """
        自动找到被删除的 .ibd 并保存到 output_path。

        优先级：
        1. /proc/<pid>/fd 中的 (deleted) 句柄
        2. /proc/<pid>/map_files 中的内存映射
        """
        pids = cls.find_mysqld_pids()
        if not pids:
            logging.warning("未找到 mysqld 进程")
            logging.warning("请确认：ps aux | grep mysqld")
            return None

        logging.info(f"找到 {len(pids)} 个 mysqld 进程: PID={pids}")

        # ── 策略1：/proc/<pid>/fd ──
        for pid in pids:
            deleted = cls.find_deleted_ibd(pid, table_hint)
            if deleted:
                logging.info(f"PID={pid} /proc/fd 中找到 {len(deleted)} 个已删除 .ibd")
                for fd_path, orig_name in deleted:
                    print(f"  [fd] {orig_name}")

                if len(deleted) > 1 and not table_hint:
                    print("\n发现多个已删除 .ibd，请用 --table 指定表名过滤：")
                    for _, orig in deleted:
                        print(f"  --table {os.path.basename(orig).replace('.ibd','')}")
                    return None

                fd_path, orig_name = deleted[0]
                return cls._copy_fd(fd_path, output_path)

        # ── 策略2：/proc/<pid>/map_files ──
        for pid in pids:
            mapped = cls.find_mapped_deleted_ibd(pid, table_hint)
            if mapped:
                logging.info(f"PID={pid} map_files 中找到 {len(mapped)} 个已删除 .ibd 映射")
                for map_path, orig_name in mapped:
                    print(f"  [map] {orig_name}")

                if len(mapped) > 1 and not table_hint:
                    print("\n发现多个内存映射 .ibd，请用 --table 指定表名过滤：")
                    for _, orig in mapped:
                        print(f"  --table {os.path.basename(orig).replace('.ibd','')}")
                    return None

                map_path, orig_name = mapped[0]
                # 尝试通过 /proc/<pid>/mem 读取
                data = cls._try_read_mapped_file(map_path)
                if data:
                    try:
                        with open(output_path, 'wb') as f:
                            f.write(data)
                        logging.info(f"从内存映射抢救完成: {len(data)/1024/1024:.1f} MB → {output_path}")
                        return output_path
                    except Exception as e:
                        logging.error(f"写入失败: {e}")
                        return None
                else:
                    logging.warning(f"无法从内存映射读取数据")

        # ── 均失败，给出诊断 ──
        print()
        print("=" * 60)
        print("  未找到可恢复的数据源")
        print("=" * 60)
        print()
        print("原因分析：")
        print("  MySQL DROP TABLE 会立即关闭 .ibd 文件句柄并释放内存映射。")
        print("  因此 /proc/<pid>/fd 和 /proc/<pid>/map_files 通常找不到数据。")
        print()
        print("恢复方案：")
        print("  - 裸盘扫描（数据仍在磁盘上，只是文件系统元数据被释放）：")
        print(f"    python {sys.argv[0]} --detect-device")
        print(f"    python {sys.argv[0]} --device /dev/vda3 --quick-scan --workers 8")
        print(f"    python {sys.argv[0]} --device /dev/vda3 --schema schema.json --workers 8 --relaxed -o recovered.sql")
        print()
        print("  或者使用 --auto-schema 自动从 SDI 页提取表结构：")
        print(f"    python {sys.argv[0]} --device /dev/vda3 --auto-schema --table audit_logs -o recovered.sql")
        print()
        return None

    @staticmethod
    def _copy_fd(fd_path: str, output_path: str) -> Optional[str]:
        """从文件描述符复制数据到文件"""
        try:
            size = 0
            with open(fd_path, 'rb') as src, open(output_path, 'wb') as dst:
                while True:
                    chunk = src.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
                    size += len(chunk)
            logging.info(f"抢救完成: {size/1024/1024:.1f} MB → {output_path}")
            return output_path
        except PermissionError:
            logging.error("读取 /proc/fd 失败：需要 root 权限")
            return None
        except Exception as e:
            logging.error(f"读取失败: {e}")
            return None


# ─────────────────────────────────────────────────────────────────
# SDI 自动提取器：从 MySQL 8.0 SDI 页提取表结构
# ─────────────────────────────────────────────────────────────────

class SDIExtractor:
    """
    从 MySQL 8.0 .ibd 文件或裸设备中提取 SDI（Serialized Dictionary
    Information）JSON，自动获取表结构定义。

    MySQL 8.0 在每个独立表空间的 .ibd 文件中存储 SDI 页（页类型 0x0045）。
    SDI 页包含 zlib 压缩的 JSON，记录完整的表定义：
      - 列名、类型、长度
      - 是否可空、是否无符号
      - 字符集
      - 索引信息

    使用场景：
      1. --auto-schema：从 .ibd 或裸设备自动提取 schema，无需手写 schema.json
      2. 裸盘恢复时先扫描 SDI 页获取表结构，再扫描 INDEX 页恢复数据
    """

    # SDI JSON 中包含的关键字段路径
    # dd_object.columns[] → {name, type, is_nullable, is_unsigned,
    #                        char_length, numeric_precision, collation_id, ...}

    @staticmethod
    def _find_sdi_pages_from_file(filepath: str, page_types: set = None) -> List[Tuple[int, bytes]]:
        """
        从 .ibd 文件或设备中提取所有 SDI 页（类型 0x0045）。

        返回 [(page_offset, raw_16kb_page), ...]
        """
        if page_types is None:
            page_types = {FIL_PAGE_SDI}  # 0x0045

        results = []
        page_sz = UNIV_PAGE_SIZE
        # 每次读 64MB 大块
        chunk_pages = (64 * 1024 * 1024) // page_sz

        with open(filepath, 'rb') as f:
            offset = 0
            while True:
                chunk = f.read(chunk_pages * page_sz)
                if not chunk:
                    break
                n = len(chunk) // page_sz
                for i in range(n):
                    base = i * page_sz
                    if base + 26 > len(chunk):
                        break
                    pt = struct.unpack_from('>H', chunk, base + FIL_PAGE_TYPE)[0]
                    if pt in page_types:
                        page_data = chunk[base:base + page_sz]
                        if len(page_data) == page_sz:
                            results.append((offset + base, bytes(page_data)))
                offset += n * page_sz

        return results

    @staticmethod
    def _decompress_sdi(raw_page: bytes) -> Optional[str]:
        """
        从 SDI 页中解压 JSON。

        MySQL 8.0 的 SDI 页结构：
          - FIL 头（38 字节）
          - 页数据区：包含 zlib 压缩的 JSON BLOB

        解压策略：
        1. 在页数据区搜索 zlib magic bytes（0x78 0x01 / 0x78 0x9C / 0x78 0xDA）
        2. 从 magic byte 位置开始尝试 zlib 解压
        3. 验证解压结果是否为有效 JSON
        """
        # Zlib magic bytes
        zlib_magics = [b'\x78\x01', b'\x78\x9C', b'\x78\xDA', b'\x78\x5E']

        # 搜索范围：FIL_PAGE_DATA (38) 到页末
        search_start = FIL_PAGE_DATA
        search_end = UNIV_PAGE_SIZE - 4

        best_json = None
        best_len = 0

        for magic in zlib_magics:
            pos = search_start
            while pos < search_end:
                found = raw_page.find(magic, pos)
                if found == -1:
                    break

                # 尝试从该位置解压
                try:
                    decompressed = zlib.decompress(raw_page[found:])
                    text = decompressed.decode('utf-8', errors='replace')

                    # 验证是否为有效 SDI JSON
                    if text.strip().startswith('{') and '"dd_object_type"' in text:
                        if len(text) > best_len:
                            # 尝试解析 JSON 确认有效性
                            try:
                                json.loads(text)
                                best_json = text
                                best_len = len(text)
                            except json.JSONDecodeError:
                                pass
                except (zlib.error, UnicodeDecodeError):
                    pass

                pos = found + 1

        return best_json

    @staticmethod
    def _parse_sdi_columns(sdi_json: Dict) -> Optional[Tuple[str, str, List[Dict]]]:
        """
        从 SDI JSON 解析表结构。

        返回 (table_name, row_format, columns_info_list)
        其中 columns_info_list 每项为 {name, type, nullable, unsigned, charset}

        SDI JSON 结构示例：
        {
          "dd_object": {
            "name": "audit_logs",
            "columns": [
              {
                "name": "id",
                "type": 16,              # MYSQL_TYPE_LONGLONG
                "is_nullable": false,
                "is_unsigned": true,
                "char_length": 20,
                "numeric_precision": 0,
                "collation_id": 0,
                "is_explicit_collation": false
              },
              {
                "name": "user_name",
                "type": 16,              # MYSQL_TYPE_VARCHAR → 实际上是 MYSQL_TYPE_STRING?
                "is_nullable": true,
                "char_length": 64,
                "collation_id": 45       # utf8mb4_general_ci
              },
              ...
            ],
            "options": "avg_row_length=0;key_block_size=0;...",
            "partition_type": 0,
            "row_format": 2,             # 2=DYNAMIC
            ...
          }
        }
        """
        # MySQL internal type codes → SQL type name
        # From MySQL source: include/mysql/com.h enum_field_types
        MYSQL_TYPE_MAP_SDI = {
            0:   ('decimal', 0),
            1:   ('tinyint', 1),
            2:   ('smallint', 2),
            3:   ('int', 4),
            4:   ('float', 4),
            5:   ('double', 8),
            6:   ('null', 0),
            7:   ('timestamp', 4),
            8:   ('bigint', 8),
            9:   ('mediumint', 3),
            10:  ('date', 3),
            11:  ('time', 3),
            12:  ('datetime', 8),
            13:  ('year', 1),
            14:  ('newdate', 3),
            15:  ('varchar', 0),
            16:  ('bit', 0),
            17:  ('timestamp2', 4),
            18:  ('datetime2', 8),
            19:  ('time2', 3),
            245: ('json', 0),
            246: ('decimal', 0),
            247: ('enum', 0),
            248: ('set', 0),
            249: ('tinyblob', 0),
            250: ('mediumblob', 0),
            251: ('longblob', 0),
            252: ('blob', 0),
            253: ('varchar', 0),
            254: ('char', 0),
            255: ('geometry', 0),
        }

        # Row format mapping
        ROW_FORMAT_MAP = {0: 'REDUNDANT', 1: 'COMPACT', 2: 'DYNAMIC', 3: 'COMPRESSED'}

        try:
            dd = sdi_json.get('dd_object', {})
            table_name = dd.get('name', 'unknown')
            row_fmt_int = dd.get('row_format', 2)
            row_format = ROW_FORMAT_MAP.get(row_fmt_int, 'DYNAMIC')

            columns_info = []
            for col in dd.get('columns', []):
                col_type = col.get('type', 253)
                type_info = MYSQL_TYPE_MAP_SDI.get(col_type, ('varchar', 0))
                type_name = type_info[0]
                char_len = col.get('char_length', 0)

                # 构建 MySQL type 字符串
                if type_name in ('varchar', 'char', 'varbinary', 'binary'):
                    if char_len > 0:
                        type_str = f'{type_name}({char_len})'
                    else:
                        type_str = type_name
                elif type_name in ('decimal',):
                    prec = col.get('numeric_precision', 10) or 10
                    scale = col.get('numeric_scale', 0) or 0
                    type_str = f'decimal({prec},{scale})'
                elif type_name in ('tinyblob', 'mediumblob', 'longblob', 'blob'):
                    type_str = type_name
                elif type_name in ('enum', 'set'):
                    # 无法从 SDI 获取枚举值，标记为基础类型
                    type_str = type_name
                else:
                    type_str = type_name

                # 字符集检测
                charset = 'utf8mb4'
                collation_id = col.get('collation_id', 0)
                if collation_id:
                    # 常见 collation id 映射
                    COLLATION_MAP = {
                        8: 'latin1', 9: 'latin1',
                        33: 'utf8mb3', 45: 'utf8mb4', 46: 'utf8mb4',
                        63: 'binary', 83: 'utf8', 192: 'utf8mb3',
                        224: 'utf8mb4', 225: 'utf8mb4',
                    }
                    charset = COLLATION_MAP.get(collation_id, 'utf8mb4')

                nullable = col.get('is_nullable', True)
                unsigned = col.get('is_unsigned', False)

                columns_info.append({
                    'name': col.get('name', f'col_{len(columns_info)}'),
                    'type': type_str,
                    'nullable': nullable,
                    'unsigned': unsigned,
                    'charset': charset,
                })

            return table_name, row_format, columns_info

        except Exception as e:
            logging.error(f"解析 SDI JSON 失败: {e}")
            return None

    @classmethod
    def extract_schema_from_file(cls, filepath: str,
                                  table_filter: str = '') -> Optional[Tuple[str, str, List[Dict]]]:
        """
        从 .ibd 文件或设备中提取所有 SDI 表结构。
        如果指定 table_filter，只返回匹配的表。

        返回 (table_name, row_format, columns_info_list)
        """
        sdi_pages = cls._find_sdi_pages_from_file(filepath, {FIL_PAGE_SDI})

        if not sdi_pages:
            logging.info(f"未在 {filepath} 中找到 SDI 页")
            return None

        logging.info(f"找到 {len(sdi_pages)} 个 SDI 页")

        for offset, raw_page in sdi_pages:
            json_text = cls._decompress_sdi(raw_page)
            if json_text is None:
                continue

            try:
                sdi_obj = json.loads(json_text)
            except json.JSONDecodeError:
                continue

            result = cls._parse_sdi_columns(sdi_obj)
            if result is None:
                continue

            table_name, row_format, columns_info = result
            if table_filter and table_filter.lower() not in table_name.lower():
                continue

            logging.info(f"SDI 提取成功: 表={table_name}, "
                         f"行格式={row_format}, 列数={len(columns_info)}")
            for ci in columns_info:
                logging.debug(f"  {ci['name']}: {ci['type']} "
                              f"{'UNSIGNED' if ci['unsigned'] else ''} "
                              f"{'NULL' if ci['nullable'] else 'NOT NULL'} "
                              f"{ci['charset']}")

            return table_name, row_format, columns_info

        logging.warning(f"未能从 SDI 页中提取表结构{' (filter=' + table_filter + ')' if table_filter else ''}")
        return None

    @classmethod
    def extract_schema_from_device(cls, device: str, table_filter: str = '',
                                    offset_mb: int = 0, length_mb: int = 0,
                                    workers: int = 4) -> Optional[Tuple[str, str, List[Dict]]]:
        """
        从裸块设备扫描 SDI 页并提取表结构。

        先快速预扫描找 SDI 候选位置，再深度解压。
        """
        # 使用 CandidateScanner 快速定位 SDI 页
        scanner = CandidateScanner(
            device=device,
            read_chunk_mb=64,
            page_types=[FIL_PAGE_SDI],
        )

        start_bytes = offset_mb * 1024 * 1024
        length_bytes = length_mb * 1024 * 1024 if length_mb else 0
        candidates = scanner.quick_scan(
            start_byte=start_bytes,
            length_bytes=length_bytes,
            workers=workers,
        )

        if not candidates:
            logging.warning(f"未在设备 {device} 上找到 SDI 页")
            return None

        logging.info(f"找到 {len(candidates)} 个 SDI 候选页，开始提取表结构...")

        page_sz = UNIV_PAGE_SIZE
        for byte_off, _, _ in candidates:
            try:
                with open(device, 'rb') as f:
                    f.seek(byte_off)
                    raw_page = f.read(page_sz)
                if len(raw_page) < page_sz:
                    continue

                json_text = cls._decompress_sdi(raw_page)
                if json_text is None:
                    continue

                try:
                    sdi_obj = json.loads(json_text)
                except json.JSONDecodeError:
                    continue

                result = cls._parse_sdi_columns(sdi_obj)
                if result is None:
                    continue

                table_name, row_format, columns_info = result
                if table_filter and table_filter.lower() not in table_name.lower():
                    continue

                logging.info(f"SDI 提取成功: 表={table_name}, "
                             f"行格式={row_format}, 列数={len(columns_info)}")
                return table_name, row_format, columns_info

            except Exception as e:
                logging.debug(f"SDI 页 offset={byte_off} 解析失败: {e}")
                continue

        logging.warning("未能从任何 SDI 页提取表结构")
        return None

    @classmethod
    def generate_schema_json(cls, table_name: str, row_format: str,
                              columns_info: List[Dict], output_path: str):
        """将提取的表结构写入 schema.json 文件"""
        schema = {
            'table': table_name,
            'row_format': row_format,
            'columns': columns_info,
            '_auto_generated': True,
            '_source': 'MySQL 8.0 SDI',
        }
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(schema, f, indent=2, ensure_ascii=False)
        logging.info(f"Schema 已写入: {output_path}")

    @classmethod
    def columns_to_columndefs(cls, columns_info: List[Dict]) -> List:
        """将 SDI 提取的列信息转换为 ColumnDef 对象列表"""
        coldefs = []
        for ci in columns_info:
            type_full = ci['type']
            length = 0
            if '(' in type_full:
                try:
                    length_str = type_full.split('(')[1].rstrip(')')
                    if ',' in length_str:
                        prec = int(length_str.split(',')[0].strip())
                        length = (prec // 2) + 1 + 4
                    else:
                        length = int(length_str.strip())
                except Exception:
                    length = 0

            coldefs.append(ColumnDef(
                name=ci['name'],
                type_str=type_full,
                length=length,
                nullable=ci.get('nullable', True),
                unsigned=ci.get('unsigned', False),
                charset=ci.get('charset', 'utf8mb4'),
            ))
        return coldefs


# ─────────────────────────────────────────────────────────────────
# 轻量级预扫描器：快速定位 InnoDB 页的候选位置
# ─────────────────────────────────────────────────────────────────

# InnoDB 页类型特征值（偏移 24-25，大端序）
INNODB_PAGE_TYPES = {
    0x45BF: 'INDEX',
    0x45BE: 'RTREE',
    0x45BD: 'UNDO_LOG',
    0x0045: 'SDI',
    0x000A: 'BLOB',
    0x000B: 'ZBLOB',
    0x000C: 'ZBLOB2',
    0x0002: 'UNDO',
    0x0003: 'INODE',
    0x0006: 'SYS',
    0x0007: 'TRX_SYS',
    0x0008: 'FSP_HDR',
    0x0009: 'XDES',
}

# InnoDB 校验和算法 (crc32 magic: 0x6854C878 / 0x87785768)
CRC32_MAGIC = 0x87785768  # 小端序存储
INNODB_MAGIC_N = 0  # none checksum magic

class CandidateScanner:
    """
    轻量级预扫描：只读每 16KB 边界的页类型（2 字节）+ 校验和（4 字节），
    快速找出可能是 InnoDB 页的候选位置。

    阶段1（快速）：按 16KB 步长扫描，只读 6 字节/16KB（页类型 + 校验和）
    阶段2（深度）：对候选位置读取完整 16KB 并解析记录
    """

    def __init__(self, device: str, read_chunk_mb: int = 64,
                 page_types: List[int] = None,
                 require_checksum: bool = False):
        self.device = device
        self.chunk_size = read_chunk_mb * 1024 * 1024
        self.page_sz = UNIV_PAGE_SIZE
        self.page_types = page_types or [FIL_PAGE_INDEX, 0x000A]  # 默认找 INDEX + BLOB
        self.require_checksum = require_checksum  # 快速模式关闭，深度模式可选
        self._size = os.path.getsize(device)

    @property
    def total_pages(self) -> int:
        return self._size // self.page_sz

    def quick_scan(self, start_byte: int = 0, length_bytes: int = 0,
                   workers: int = 4) -> List[Tuple[int, int, int]]:
        """
        快速预扫描：多线程检查每个 16KB 边界。

        返回 [(byte_offset, page_type, checksum), ...]
        每个候选只需读取 6 字节（页类型2B + 校验和4B），而不是 16KB。

        workers: 并行扫描线程数
        """
        if length_bytes <= 0:
            length_bytes = self._size - start_byte
        end_byte = start_byte + length_bytes
        if end_byte > self._size:
            end_byte = self._size

        total = (end_byte - start_byte) // self.page_sz
        logging.info(f"预扫描: {total:,} 页候选 ({length_bytes/1024**3:.2f} GB)，{workers} 线程并行")

        # 分块
        chunk_pages = total // workers + 1
        chunks = []
        for i in range(workers):
            cs = start_byte + i * chunk_pages * self.page_sz
            ce = min(cs + chunk_pages * self.page_sz, end_byte)
            if cs >= ce:
                break
            chunks.append((cs, ce))

        candidates = []
        t0 = time.time()
        pages_done = [0]

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(self._quick_scan_chunk, cs, ce, pages_done): (cs, ce)
                      for cs, ce in chunks}

            for fut in as_completed(futures):
                results = fut.result()
                candidates.extend(results)

        elapsed = time.time() - t0
        rate = total / elapsed if elapsed > 0 else 0
        logging.info(f"预扫描完成: 耗时 {elapsed:.1f}s，"
                     f"速率 {rate:,.0f} 页/秒，"
                     f"候选 {len(candidates):,} 个")

        # 按类型统计
        type_counts = {}
        for _, pt, _ in candidates:
            name = INNODB_PAGE_TYPES.get(pt, f'0x{pt:04X}')
            type_counts[name] = type_counts.get(name, 0) + 1
        for name, cnt in sorted(type_counts.items(), key=lambda x: -x[1]):
            logging.info(f"  {name:<12} {cnt:>8,} 页")

        return candidates

    def _quick_scan_chunk(self, start_byte: int, end_byte: int,
                          pages_done: list) -> List[Tuple[int, int, int]]:
        """扫描一个块，返回候选位置列表"""
        results = []
        page_sz = self.page_sz
        buf_size = self.chunk_size
        # 确保 buf_size 是 page_sz 的整数倍
        buf_pages = buf_size // page_sz
        buf_size = buf_pages * page_sz

        type_set = set(self.page_types)

        with open(self.device, 'rb') as f:
            f.seek(start_byte)
            offset = start_byte
            while offset < end_byte:
                read_size = min(buf_size, end_byte - offset)
                chunk = f.read(read_size)
                if not chunk:
                    break
                n = len(chunk) // page_sz
                for i in range(n):
                    p_off = offset + i * page_sz
                    base = i * page_sz
                    if base + 28 > len(chunk):
                        break
                    page_type = struct.unpack_from('>H', chunk, base + FIL_PAGE_TYPE)[0]
                    if page_type in type_set:
                        checksum = struct.unpack_from('>I', chunk, base + FIL_PAGE_SPACE_OR_CHKSUM)[0]
                        results.append((p_off, page_type, checksum))
                    pages_done[0] += 1
                offset += n * page_sz

                # 进度报告（只由线程0报告，避免竞争）
                if pages_done[0] % 500000 < n:
                    pass  # 由主线程统一报告

        return results


# ─────────────────────────────────────────────────────────────────
# 裸块设备扫描器 v2：预扫描 + 多线程深度解析
# ─────────────────────────────────────────────────────────────────

class RawDeviceScanner:
    """
    直接扫描块设备（/dev/sda1、/dev/vda3 等）或磁盘镜像文件，
    按 UNIV_PAGE_SIZE(16KB) 步长寻找 InnoDB INDEX 页。

    改进 v2:
    - 预扫描模式（--quick-scan）：只读页类型快速定位候选区
    - 多线程深度解析（--workers N）
    - 宽松检测模式（--relaxed）：不过滤 page_level，放宽 n_heap 限制
    - 增量进度（速率 + ETA）
    """

    PAGE_TYPE_OFFSET = FIL_PAGE_TYPE

    def __init__(self, device: str, columns: List[ColumnDef],
                 row_format: str = 'DYNAMIC',
                 include_deleted: bool = True,
                 space_id: int = 0,
                 read_chunk_mb: int = 64,
                 workers: int = 4,
                 relaxed: bool = False,
                 candidates: List[Tuple[int, int, int]] = None):
        self.device = device
        self.columns = columns
        self.row_format = row_format.upper()
        self.include_deleted = include_deleted
        self.space_id = space_id
        self.chunk_size = read_chunk_mb * 1024 * 1024
        self.workers = workers
        self.relaxed = relaxed
        self._candidates = candidates  # 预扫描结果

        if not os.path.exists(device):
            raise FileNotFoundError(f"设备/文件不存在: {device}")

        self._size = os.path.getsize(device)

    def _is_innodb_index_page(self, raw: bytes) -> Tuple[bool, str]:
        """
        判断是否为可恢复的 InnoDB 页。
        返回 (is_valid, reason)。

        宽松模式：接受所有 INDEX 页 + BLOB 页，不限 level，n_heap 上限 50000。
        严格模式：只接受 level==0，n_heap 2-2000。
        """
        if len(raw) < UNIV_PAGE_SIZE:
            return False, "too_short"

        page_type = read_u16_be(raw, FIL_PAGE_TYPE)

        # 接受 INDEX 页和 BLOB 页（BLOB 页含溢出字段数据）
        if page_type == FIL_PAGE_INDEX:
            pass
        elif page_type in (0x000A, 0x000B, 0x000C):  # BLOB/ZBLOB/ZBLOB2
            return True, "BLOB"
        else:
            return False, f"type=0x{page_type:04X}"

        # 过滤 space_id
        if self.space_id:
            sid = read_u32_be(raw, FIL_PAGE_ARCH_LOG_NO)
            if sid != self.space_id:
                return False, "space_id_mismatch"

        # 页级别检查
        level = read_u16_be(raw, FIL_PAGE_DATA + PAGE_LEVEL)

        # N_HEAP 合法性
        n_heap_raw = read_u16_be(raw, FIL_PAGE_DATA + PAGE_N_HEAP)
        n_heap = n_heap_raw & 0x7FFF

        if self.relaxed:
            if n_heap < 2 or n_heap > 50000:
                return False, f"n_heap={n_heap}"
        else:
            if n_heap < 2 or n_heap > 2000:
                return False, f"n_heap={n_heap}"
            if level != 0:
                return False, f"level={level}"

        # 额外验证：heap_top 应合理
        heap_top = read_u16_be(raw, FIL_PAGE_DATA + PAGE_HEAP_TOP)
        if heap_top < FIL_PAGE_DATA + PAGE_HEADER_SIZE + 38:
            return False, "heap_top_too_low"
        if heap_top > UNIV_PAGE_SIZE:
            return False, "heap_top_overflow"

        return True, f"level={level}"

    def _parse_one_page(self, raw: bytes, byte_offset: int) -> Tuple[List[Dict], int]:
        """深度解析一页，返回 (rows, page_type)"""
        page_type = read_u16_be(raw, FIL_PAGE_TYPE)
        page_no = byte_offset // UNIV_PAGE_SIZE
        rows = []

        try:
            page = InnoDBPage(raw, page_no)
            parser = RecordParser(page, self.columns,
                                  include_deleted=self.include_deleted)

            if page_type == FIL_PAGE_INDEX:
                page_rows = parser.scan_page()
                if not page_rows:
                    page_rows = parser.scan_page_brute_force()
                for r in page_rows:
                    r['page_no'] = page_no
                    r['device_offset'] = byte_offset
                rows = page_rows
            elif page_type in (0x000A, 0x000B, 0x000C):
                # BLOB 页：尝试暴力扫描（部分文本/VARCHAR 溢出数据）
                page_rows = parser.scan_page_brute_force()
                for r in page_rows:
                    r['page_no'] = page_no
                    r['device_offset'] = byte_offset
                    r['_is_blob_page'] = True
                rows = page_rows
        except Exception as e:
            logging.debug(f"解析页 offset={byte_offset} 失败: {e}")

        return rows, page_type

    def _deep_scan_candidates(self, candidate_list: List[Tuple[int, int, int]]) -> List[Dict]:
        """
        多线程深度解析候选页。
        candidate_list: [(byte_offset, page_type, checksum), ...]
        """
        all_rows = []
        seen_keys = set()
        total = len(candidate_list)

        if total == 0:
            return all_rows

        logging.info(f"深度解析 {total:,} 个候选页，{self.workers} 线程并行...")

        # 创建索引页候选（优先处理）和 BLOB 页候选
        index_candidates = [c for c in candidate_list if c[1] == FIL_PAGE_INDEX]
        other_candidates = [c for c in candidate_list if c[1] != FIL_PAGE_INDEX]

        # 优先处理 INDEX 页
        all_candidates = index_candidates + other_candidates
        chunk_size = max(1, len(all_candidates) // self.workers)
        chunks = [all_candidates[i:i + chunk_size] for i in range(0, len(all_candidates), chunk_size)]

        t0 = time.time()
        processed = [0]

        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            futures = {ex.submit(self._deep_scan_chunk, chunk, processed, total): i
                      for i, chunk in enumerate(chunks)}

            for fut in as_completed(futures):
                rows_from_chunk = fut.result()
                for r in rows_from_chunk:
                    key = IBDScanner._row_key(r['row'])
                    if key not in seen_keys:
                        seen_keys.add(key)
                        all_rows.append(r)

        elapsed = time.time() - t0
        pps = total / elapsed if elapsed > 0 else 0
        logging.info(f"深度解析完成: 耗时 {elapsed:.1f}s，"
                     f"速率 {pps:,.0f} 页/秒，"
                     f"恢复 {len(all_rows)} 条记录")
        return all_rows

    def _deep_scan_chunk(self, candidates: List[Tuple[int, int, int]],
                         processed: list, total: int) -> List[Dict]:
        """深度解析一批候选页（在线程中运行）"""
        results = []
        page_sz = UNIV_PAGE_SIZE

        with open(self.device, 'rb') as f:
            for byte_off, page_type, _checksum in candidates:
                try:
                    f.seek(byte_off)
                    raw = f.read(page_sz)
                    if len(raw) < page_sz:
                        continue

                    is_valid, reason = self._is_innodb_index_page(raw)
                    if not is_valid:
                        continue

                    rows, _ = self._parse_one_page(raw, byte_off)
                    results.extend(rows)
                except Exception:
                    pass

                processed[0] += 1
                # 每处理 1000 个报告一次进度
                if processed[0] % 1000 == 0:
                    logging.debug(f"深度解析进度: {processed[0]:,}/{total:,}")

        return results

    def scan(self) -> List[Dict]:
        """
        扫描整个设备。

        流程：
        1. 如果没有候选列表 → 先做预扫描
        2. 对候选页做多线程深度解析（自动过滤无效页）
        """
        if self._candidates is None:
            scanner = CandidateScanner(
                device=self.device,
                read_chunk_mb=self.chunk_size // (1024 * 1024),
                page_types=[FIL_PAGE_INDEX, 0x000A, 0x000B, 0x000C],
            )
            self._candidates = scanner.quick_scan(workers=self.workers)

        logging.info(f"预扫描候选 {len(self._candidates):,} 个 → "
                     f"进入深度解析")
        return self._deep_scan_candidates(self._candidates)

    def scan_range(self, start_byte: int, length_bytes: int) -> List[Dict]:
        """
        只扫描指定字节范围。
        """
        scanner = CandidateScanner(
            device=self.device,
            read_chunk_mb=self.chunk_size // (1024 * 1024),
            page_types=[FIL_PAGE_INDEX, 0x000A, 0x000B, 0x000C],
        )
        candidates = scanner.quick_scan(
            start_byte=start_byte,
            length_bytes=length_bytes,
            workers=self.workers,
        )

        logging.info(f"范围 {start_byte/1024**2:.0f}MB - "
                     f"{(start_byte+length_bytes)/1024**2:.0f}MB: "
                     f"候选 {len(candidates)} 个")
        return self._deep_scan_candidates(candidates)


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

【场景2】mysqld 已重启 / .ibd 彻底删除 — 裸盘扫描 + SDI 自动提取（推荐）：
  # 一步到位（自动从 SDI 提取表结构，无需手写 schema.json）
  python innodb_recovery.py --device /dev/vda3 --auto-schema --table orders \\
      --workers 8 --relaxed -o recovered.sql
  # 或先预扫描定位，再精准恢复
  python innodb_recovery.py --device /dev/vda3 --quick-scan --workers 8
  python innodb_recovery.py --device /dev/vda3 --auto-schema --table orders \\
      --offset 18300 --length 1200 --workers 8 --relaxed -o recovered.sql

【场景3】手动 schema 方式裸盘扫描（传统方式）：
  python innodb_recovery.py --device /dev/sda1 --schema schema.json \\
      --workers 8 --relaxed -o out.sql

【场景4】检测设备 & 生成 schema 模板：
  python innodb_recovery.py --detect-device
  python innodb_recovery.py --gen-schema orders --schema-out schema.json

【场景5】查看页分布（.ibd 存在时）：
  python innodb_recovery.py --ibd orders.ibd --page-info

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
    parser.add_argument('--auto-schema', action='store_true',
                        help='自动从 MySQL 8.0 SDI 页提取表结构（无需手动写 schema.json）')
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
    parser.add_argument('--workers',  type=int, default=4,
                        help='并行扫描线程数（默认4）')
    parser.add_argument('--quick-scan', action='store_true',
                        help='快速预扫描模式：只定位 InnoDB 页位置，不做深度恢复（用于评估）')
    parser.add_argument('--relaxed', action='store_true',
                        help='宽松检测模式：接受所有 INDEX/BLOB 页（不限 level），放宽 n_heap 限制')

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

    # ── 快速预扫描（不需要 schema） ──
    if args.quick_scan and args.device:
        scanner_pre = CandidateScanner(
            device=args.device,
            read_chunk_mb=args.read_chunk,
            page_types=[FIL_PAGE_INDEX, 0x000A, 0x000B, 0x000C],
        )
        start_bytes = args.offset * 1024 * 1024
        length_bytes = args.length * 1024 * 1024 if args.length else 0
        candidates = scanner_pre.quick_scan(
            start_byte=start_bytes,
            length_bytes=length_bytes,
            workers=args.workers,
        )

        if candidates:
            print(f"\n找到 {len(candidates)} 个候选 InnoDB 页。")
            print(f"\n使用以下命令进行恢复（已自动建议 --offset 跳过无关区域）：")
            first = candidates[0][0]
            last  = candidates[-1][0]
            start_mb = max(0, first // 1024 // 1024 - 100)
            scan_len  = max(256, (last - first) // 1024 // 1024 + 200)
            print(f"  python {sys.argv[0]} --device {args.device} \\")
            print(f"      --schema <schema.json> \\")
            print(f"      --offset {start_mb} --length {scan_len} \\")
            print(f"      --workers {args.workers} --relaxed -o recovered.sql")
            print(f"\n  候选页位置分布:")
            seg_size = 1024 * 1024 * 1024
            segs = {}
            for off, pt, _ in candidates:
                seg = off // seg_size
                name = INNODB_PAGE_TYPES.get(pt, f'0x{pt:04X}')
                segs[seg] = segs.get(seg, {})
                segs[seg][name] = segs[seg].get(name, 0) + 1
            for seg in sorted(segs):
                types_str = ', '.join(f'{k}:{v}' for k, v in sorted(segs[seg].items()))
                print(f"    {seg}-{seg+1} GB: {types_str}")
        else:
            print(f"\n未找到 InnoDB 页特征。可能原因：")
            print(f"  1. 扫描范围 ({start_bytes/1024**2:.0f}-{(start_bytes+length_bytes)/1024**2:.0f} MB) 不含数据")
            print(f"  2. 数据已被覆盖")
            print(f"  3. 设备路径不正确")
            print(f"\n建议：扩大扫描范围或确认正确设备")
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

    # ── 检查 schema（--auto-schema 可从数据源自动提取）──
    if not args.schema and not args.gen_schema and not args.auto_schema:
        parser.print_help()
        print("\n错误: 需要 --schema 参数，或使用 --auto-schema 自动提取表结构")
        print("提示: --auto-schema 从 MySQL 8.0 SDI 页自动获取表定义，无需手动写 schema.json")
        sys.exit(1)

    # ── 加载/提取 schema ──
    if args.auto_schema:
        # 从 SDI 页自动提取表结构
        if args.ibd:
            logging.info(f"从 {args.ibd} 的 SDI 页提取表结构...")
            result = SDIExtractor.extract_schema_from_file(args.ibd, args.table or '')
        elif args.device:
            logging.info(f"从 {args.device} 的 SDI 页提取表结构...")
            result = SDIExtractor.extract_schema_from_device(
                args.device, args.table or '',
                offset_mb=args.offset, length_mb=args.length,
                workers=args.workers,
            )
        elif args.rescue:
            # rescue 模式先抢救 .ibd，再提取 schema
            table_hint = args.table or 'recovered'
            rescued_path = f'/tmp/rescued_{table_hint}.ibd'
            rescued = ProcFdRescuer.rescue(rescued_path, table_hint)
            if rescued is None:
                logging.warning("rescue 失败，无法自动提取 schema")
                print("\n无法获取数据源，请使用裸盘扫描：")
                print(f"  python {sys.argv[0]} --detect-device")
                print(f"  python {sys.argv[0]} --device /dev/vda3 --auto-schema --table {args.table or 'your_table'} -o recovered.sql")
                sys.exit(1)
            logging.info(f"从抢救的文件提取 schema: {rescued}")
            result = SDIExtractor.extract_schema_from_file(rescued, args.table or '')
        else:
            logging.error("--auto-schema 需要配合 --ibd、--device 或 --rescue 使用")
            sys.exit(1)

        if result is None:
            logging.error("SDI 提取失败。请手动创建 schema.json")
            print()
            print("可能原因：")
            print("  1. .ibd 文件中没有 SDI 页（共享表空间或旧版本 MySQL）")
            print("  2. 数据已被覆盖")
            print("  3. 表名不匹配（用 --table 指定）")
            print()
            print("手动生成 schema 模板：")
            print(f"  python {sys.argv[0]} --gen-schema your_table")
            sys.exit(1)

        table_name, row_format, cols_info = result
        columns = SDIExtractor.columns_to_columndefs(cols_info)

        # 可选：保存提取的 schema 到文件
        if args.schema_out:
            SDIExtractor.generate_schema_json(table_name, row_format, cols_info, args.schema_out)

        logging.info(f"SDI 表结构: {table_name}  行格式: {row_format}  列数: {len(columns)}")
        for ci in cols_info:
            logging.debug(f"  {ci['name']}: {ci['type']} "
                         f"{'UNSIGNED' if ci.get('unsigned') else ''} "
                         f"{'NULL' if ci.get('nullable') else 'NOT NULL'}")

    else:
        # 从 JSON 文件加载 schema
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
            print()
            print("=" * 60)
            print("  /proc/fd 和 map_files 均未找到可恢复的数据")
            print("=" * 60)
            print()
            print("原因: MySQL DROP TABLE 会立即关闭 .ibd 文件句柄并释放内存映射。")
            print("      数据仍在磁盘上，只是文件系统元数据被释放了。")
            print()
            print("推荐方案 — 裸盘扫描 + 自动提取 schema：")
            print(f"  1. 检测设备:    python {sys.argv[0]} --detect-device")
            print(f"  2. 快速预扫描:  python {sys.argv[0]} --device /dev/vda3 --quick-scan --workers 8")
            print(f"  3. 自动恢复:    python {sys.argv[0]} --device /dev/vda3 \\")
            print(f"                      --auto-schema --table {table_hint} \\")
            print(f"                      --workers 8 --relaxed -o recovered.sql")
            print()
            print("如果知道设备路径，也可一步完成：")
            print(f"  python {sys.argv[0]} --device /dev/vda3 --auto-schema \\")
            print(f"      --table {table_hint} --workers 8 --relaxed -o recovered.sql")
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
            workers=args.workers,
            relaxed=args.relaxed,
        )

        if args.offset or args.length:
            start_bytes  = args.offset * 1024 * 1024
            length_bytes = args.length * 1024 * 1024 if args.length else 0
            if length_bytes <= 0:
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
        logging.warning("  - 若使用 --device，先用 --quick-scan 预扫描定位候选页")
        logging.warning("  - 若使用 --device，加 --relaxed 放宽检测条件")
        logging.warning("  - 指定 --space-id 或 --offset/--length 缩小范围（预扫描会给出建议）")

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
