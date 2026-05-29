#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
innodb_recovery_test.py
=======================
自动测试脚本：
1. 用 Python 在内存中生成一个最小化的 MySQL 8.0 风格 INDEX 页
2. 写入几条 COMPACT 格式记录（含软删除记录）
3. 调用恢复工具解析该页，验证结果

运行方法：
  python innodb_recovery_test.py
"""

import struct
import os
import sys
import json
import zlib
import tempfile
import logging

# 把工具目录加入 path
sys.path.insert(0, os.path.dirname(__file__))
from innodb_recovery import (
    UNIV_PAGE_SIZE, FIL_PAGE_TYPE, FIL_PAGE_INDEX, FIL_PAGE_SDI,
    FIL_PAGE_DATA, PAGE_HEADER_SIZE, PAGE_N_HEAP,
    PAGE_HEAP_TOP, PAGE_LEVEL, PAGE_INDEX_ID,
    REC_N_NEW_EXTRA_BYTES, REC_INFO_DELETED_FLAG,
    REC_STATUS_ORDINARY, REC_STATUS_INFIMUM, REC_STATUS_SUPREMUM,
    InnoDBPage, RecordParser, ColumnDef, IBDScanner, OutputWriter,
    load_schema, SDIExtractor
)

logging.basicConfig(level=logging.DEBUG, format='%(levelname)s %(message)s')

# ─────────────────────────────────────────────
# 构造最小 INDEX 页（COMPACT 格式）
# ─────────────────────────────────────────────

def build_u8(v):  return bytes([v & 0xFF])
def build_u16(v): return struct.pack('>H', v & 0xFFFF)
def build_u32(v): return struct.pack('>I', v & 0xFFFFFFFF)
def build_u64(v): return struct.pack('>Q', v & 0xFFFFFFFFFFFFFFFF)

def encode_mysql_int(v: int, n: int, unsigned: bool = False) -> bytes:
    """
    MySQL 整数编码：大端 + 最高位翻转
    MySQL 存储整数时，将原值的最高字节最高位翻转（XOR 0x80）
    这样无论有符号还是无符号，存储的字节序列都能按无符号大端比较
    """
    if unsigned:
        b = v.to_bytes(n, 'big')
    else:
        # 有符号：先转为正整数范围，再翻转最高位
        if v < 0:
            v = v + (1 << (n * 8))   # 补码表示
        b = v.to_bytes(n, 'big')
    # 翻转最高字节的最高位
    ba = bytearray(b)
    ba[0] ^= 0x80
    return bytes(ba)

def encode_varchar(s: str) -> bytes:
    return s.encode('utf-8')

def build_compact_record(
    fields: list,        # [(col, val), ...]  col 是 ColumnDef，val 是 Python 值
    deleted: bool = False,
    heap_no: int = 2,
    next_offset: int = 0,   # 下一条记录相对当前原点的偏移
) -> bytes:
    """
    构造一条 COMPACT 记录的 extra+data 二进制。
    返回值：extra(5B 逆序前缀) + 变长长度列表(逆序) + NULL位图(逆序) + 系统列 + 数据
    实际写入时，记录原点 = 变长+NULL+extra 之后。
    """
    nullable_cols = [c for c, _ in fields if c.nullable]
    var_cols      = [(c, v) for c, v in fields if c.is_variable()]

    # NULL 位图（逆序存）
    n_nullable = len(nullable_cols)
    null_bitmap = bytearray((n_nullable + 7) // 8)
    null_col_idx = 0
    for c, v in fields:
        if c.nullable:
            if v is None:
                byte_i = null_col_idx // 8
                bit_i  = null_col_idx % 8
                null_bitmap[byte_i] |= (1 << bit_i)
            null_col_idx += 1
    null_bitmap_bytes = bytes(reversed(null_bitmap))

    # 变长字段长度列表（逆序存）
    var_len_list = []
    for c, v in reversed(var_cols):
        if v is None:
            var_len_list.append(0)
            continue
        raw = encode_varchar(str(v)) if isinstance(v, str) else v
        n = len(raw)
        if n < 128:
            var_len_list.append(n)
        else:
            var_len_list.append(0x80 | (n >> 8))
            var_len_list.append(n & 0xFF)
    var_len_bytes = bytes(var_len_list)

    # 5 字节 extra（紧贴原点之前）
    info_bits = REC_INFO_DELETED_FLAG if deleted else 0
    n_owned   = 0
    extra_byte0 = ((info_bits) | n_owned) & 0xFF
    heap_status = ((heap_no << 3) | REC_STATUS_ORDINARY) & 0xFFFF
    next_raw    = next_offset & 0xFFFF
    extra = bytes([extra_byte0]) + struct.pack('>H', heap_status) + struct.pack('>H', next_raw)

    # 系统列：trx_id(6) + roll_ptr(7)
    trx_id   = b'\x00' * 6
    roll_ptr = b'\x00' * 7

    # 数据
    data_parts = []
    for c, v in fields:
        if v is None:
            continue
        if c.is_variable():
            raw = encode_varchar(str(v)) if isinstance(v, str) else bytes(v)
            data_parts.append(raw)
        else:
            fl = c.fixed_len
            if c.data_type == 6:  # DATA_INT
                raw = encode_mysql_int(int(v), fl, c.unsigned)
            else:
                raw = str(v).encode('utf-8')[:fl].ljust(fl, b'\x00')
            data_parts.append(raw)

    data_bytes = b''.join(data_parts)

    # 前缀（在原点之前，逆序存放）= var_len_bytes + null_bitmap_bytes + extra
    prefix = var_len_bytes + null_bitmap_bytes + extra
    # 记录实体 = prefix + trx_id + roll_ptr + data_bytes
    # 原点 = prefix 末尾
    return prefix, trx_id + roll_ptr + data_bytes


def build_test_page(records_data):
    """
    构造一个最小合法的 16KB COMPACT INDEX 叶子页，写入若干记录。
    records_data: [ (fields_list, deleted_bool), ... ]
    """
    page = bytearray(UNIV_PAGE_SIZE)

    # ── FIL 头 ──
    struct.pack_into('>H', page, FIL_PAGE_TYPE, FIL_PAGE_INDEX)
    struct.pack_into('>I', page, 4, 1)   # page_no = 1

    # ── PAGE 头 ──
    ph = FIL_PAGE_DATA
    struct.pack_into('>H', page, ph + 4,  0x8000 | 2)  # PAGE_N_HEAP: compact + 2 sys records
    struct.pack_into('>H', page, ph + 26, 0)            # PAGE_LEVEL = 0（叶子）
    struct.pack_into('>Q', page, ph + 28, 1)            # PAGE_INDEX_ID = 1

    # ── Infimum / Supremum ──
    # infimum 原点 = FIL_PAGE_DATA + PAGE_HEADER_SIZE + 20 + 5 = 38+56+20+5 = 119
    # 实际偏移对应 MySQL 源码 PAGE_NEW_INFIMUM = 99（相对页起始）
    INF_ORIGIN = FIL_PAGE_DATA + PAGE_HEADER_SIZE + 20 + 5   # 119
    SUP_ORIGIN = INF_ORIGIN + 8 + 5                          # 132

    # infimum extra（5B 在原点之前）
    # info=0, heap_no=0, status=REC_STATUS_INFIMUM=2, next=sup_origin-inf_origin
    inf_heap_status = (0 << 3) | REC_STATUS_INFIMUM
    inf_next = SUP_ORIGIN - INF_ORIGIN  # 13
    page[INF_ORIGIN - 5] = 0
    struct.pack_into('>H', page, INF_ORIGIN - 4, inf_heap_status)
    struct.pack_into('>H', page, INF_ORIGIN - 2, inf_next & 0xFFFF)
    page[INF_ORIGIN: INF_ORIGIN+8] = b'infimum\x00'

    # supremum extra
    sup_heap_status = (1 << 3) | REC_STATUS_SUPREMUM
    page[SUP_ORIGIN - 5] = 0x10  # n_owned = 1+N（先设成1）
    struct.pack_into('>H', page, SUP_ORIGIN - 4, sup_heap_status)
    struct.pack_into('>H', page, SUP_ORIGIN - 2, 0)  # next=0
    page[SUP_ORIGIN: SUP_ORIGIN+8] = b'supremum'

    # ── 写入用户记录 ──
    heap_top = SUP_ORIGIN + 8  # 记录堆当前顶
    rec_origins = []

    built = []
    for fields, deleted in records_data:
        prefix, data = build_compact_record(fields, deleted=deleted, heap_no=2+len(built))
        built.append((prefix, data))

    # 两趟：先算偏移，再写数据
    # 先确定每条记录原点位置
    origins = []
    cur_pos = heap_top
    for prefix, data in built:
        origin = cur_pos + len(prefix)
        origins.append(origin)
        cur_pos = origin + len(data)
    heap_top_new = cur_pos

    # 设置 infimum→first record next
    if origins:
        first_next = origins[0] - INF_ORIGIN
        struct.pack_into('>H', page, INF_ORIGIN - 2, first_next & 0xFFFF)

    # 写记录，设置链
    for i, ((prefix, data), origin) in enumerate(zip(built, origins)):
        if i + 1 < len(origins):
            next_off = origins[i+1] - origin
        else:
            next_off = SUP_ORIGIN - origin
        # 修改 prefix 中的 next 字段（最后 2 字节 of extra）
        prefix = bytearray(prefix)
        struct.pack_into('>H', prefix, len(prefix)-2, next_off & 0xFFFF)
        start = origin - len(prefix)
        page[start: start + len(prefix)] = prefix
        page[origin: origin + len(data)] = data

    # 更新 PAGE_HEAP_TOP
    struct.pack_into('>H', page, FIL_PAGE_DATA + PAGE_HEAP_TOP, heap_top_new)
    # 更新 PAGE_N_HEAP
    n_heap = 0x8000 | (2 + len(built))
    struct.pack_into('>H', page, FIL_PAGE_DATA + PAGE_N_HEAP, n_heap)
    # PAGE_N_RECS
    struct.pack_into('>H', page, FIL_PAGE_DATA + 16, len(built))

    return bytes(page)


# ─────────────────────────────────────────────
# 测试执行
# ─────────────────────────────────────────────

def run_tests():
    print("=" * 60)
    print("InnoDB Recovery Tool for MySQL 8.0 - Unit Tests")
    print("=" * 60)

    # 列定义
    col_id     = ColumnDef('id',     'bigint',      8, nullable=False, unsigned=True)
    col_name   = ColumnDef('name',   'varchar(64)',  64, nullable=True,  charset='utf8mb4')
    col_age    = ColumnDef('age',    'int',          4, nullable=True,  unsigned=False)
    columns    = [col_id, col_name, col_age]

    # 构造测试记录
    records = [
        # (fields, deleted)
        ([(col_id, 1), (col_name, 'Alice'), (col_age, 30)], False),
        ([(col_id, 2), (col_name, 'Bob'),   (col_age, 25)], True),   # 软删除
        ([(col_id, 3), (col_name, None),    (col_age, None)], False), # NULL 值
    ]

    page_data = build_test_page(records)
    assert len(page_data) == UNIV_PAGE_SIZE

    page = InnoDBPage(page_data, 1)
    print(f"\nPage info: {page}")
    assert page.is_index_page(), "应为 INDEX 页"
    assert page.is_leaf(),       "应为叶子页"
    assert page.is_compact,      "应为 COMPACT 格式"

    # 扫描（含软删除）
    parser = RecordParser(page, columns, include_deleted=True)
    rows = parser.scan_page()
    print(f"\nScan result (include deleted): {len(rows)} rows")
    for r in rows:
        flag = "[DELETED]" if r['deleted'] else "[ACTIVE] "
        print(f"  {flag} heap_no={r['heap_no']} row={r['row']}")

    assert len(rows) == 3, f"Expected 3 rows, got {len(rows)}"
    assert rows[0]['row']['id'] == 1
    assert rows[0]['row']['name'] == 'Alice'
    assert rows[0]['row']['age'] == 30, f"Expected age=30, got {rows[0]['row']['age']}"
    assert rows[1]['deleted'] == True
    assert rows[2]['row']['name'] is None
    print("PASS: scan_page test")

    # 扫描（不含软删除）
    parser2 = RecordParser(page, columns, include_deleted=False)
    rows2 = parser2.scan_page()
    assert len(rows2) == 2, f"Expected 2 rows, got {len(rows2)}"
    print("PASS: filter deleted test")

    # 写入临时 .ibd 文件测试 IBDScanner
    with tempfile.NamedTemporaryFile(suffix='.ibd', delete=False) as f:
        # 写 4 页：页0(FSP)、页1(INDEX)、页2(空)、页3(INDEX)
        f.write(b'\x00' * UNIV_PAGE_SIZE)  # 页0
        f.write(page_data)                  # 页1
        f.write(b'\x00' * UNIV_PAGE_SIZE)  # 页2
        f.write(page_data)                  # 页3（重复，去重后仍3条）
        tmp_path = f.name

    scanner = IBDScanner(tmp_path, columns, row_format='COMPACT',
                         include_deleted=True, brute_force=False)
    all_rows = scanner.scan()
    print(f"\nIBDScanner result: {len(all_rows)} rows (deduped)")
    assert len(all_rows) == 3, f"Expected 3 rows, got {len(all_rows)}"
    print("PASS: IBDScanner test")

    os.unlink(tmp_path)

    # 输出格式测试
    writer = OutputWriter('test_table', columns)
    sql = writer.to_sql(all_rows)
    assert 'INSERT INTO' in sql
    assert "-- [DELETED]" in sql
    print("PASS: SQL output test")

    csv_out = writer.to_csv(all_rows)
    lines = [l for l in csv_out.strip().split('\n') if l]
    assert len(lines) == 4  # header + 3 rows
    print("PASS: CSV output test")

    j = json.loads(writer.to_json(all_rows))
    assert len(j) == 3
    print("PASS: JSON output test")

    # ── SDI extraction test ──
    print()
    sdi_json = {
        "sdi_version": 80019,
        "dd_object_type": "Table",
        "dd_object": {
            "name": "test_table",
            "row_format": 2,
            "columns": [
                {"name": "id", "type": 8, "is_nullable": False, "is_unsigned": True,
                 "char_length": 20, "numeric_precision": 0, "collation_id": 0},
                {"name": "name", "type": 15, "is_nullable": True, "is_unsigned": False,
                 "char_length": 64, "collation_id": 45},
                {"name": "score", "type": 3, "is_nullable": True, "is_unsigned": False,
                 "char_length": 11, "numeric_precision": 0, "collation_id": 0},
            ]
        }
    }
    raw_json = json.dumps(sdi_json).encode('utf-8')
    compressed = zlib.compress(raw_json)

    # 构造一个包含 SDI 数据的 16KB 页
    sdi_page = bytearray(UNIV_PAGE_SIZE)
    struct.pack_into('>H', sdi_page, FIL_PAGE_TYPE, FIL_PAGE_SDI)
    # 将压缩数据写入页数据区
    sdi_page[FIL_PAGE_DATA: FIL_PAGE_DATA + len(compressed)] = compressed

    # 测试解压
    decompressed = SDIExtractor._decompress_sdi(bytes(sdi_page))
    assert decompressed is not None, "SDI decompression failed"
    print(f"SDI decompressed: {len(decompressed)} chars")
    print(f"  Preview: {decompressed[:80]}...")

    # 测试解析
    result = SDIExtractor._parse_sdi_columns(json.loads(decompressed))
    assert result is not None, "SDI parse failed"
    table_name, row_format, cols_info = result
    assert table_name == 'test_table'
    assert row_format == 'DYNAMIC'
    assert len(cols_info) == 3
    assert cols_info[0]['name'] == 'id'
    assert cols_info[0]['type'] == 'bigint'
    assert cols_info[0]['unsigned'] == True
    assert cols_info[1]['name'] == 'name'
    assert cols_info[1]['type'] == 'varchar(64)'
    assert cols_info[2]['name'] == 'score'
    assert cols_info[2]['type'] == 'int'
    print(f"  Parsed: {table_name} ({row_format}), {len(cols_info)} columns")
    for ci in cols_info:
        print(f"    {ci['name']}: {ci['type']} unsigned={ci.get('unsigned')} nullable={ci.get('nullable')}")

    # 测试转 ColumnDef
    coldefs = SDIExtractor.columns_to_columndefs(cols_info)
    assert len(coldefs) == 3
    assert coldefs[0].name == 'id'
    assert coldefs[0].unsigned == True
    assert coldefs[0].fixed_len == 8
    assert coldefs[1].name == 'name'
    assert coldefs[1].max_len == 64
    print("PASS: SDI extraction test")

    # ── SDI from file test ──
    with tempfile.NamedTemporaryFile(suffix='.ibd', delete=False) as f:
        # 页0: SDI page
        f.write(bytes(sdi_page))
        tmp_sdi = f.name

    result2 = SDIExtractor.extract_schema_from_file(tmp_sdi, 'test_table')
    assert result2 is not None, "SDI extract from file failed"
    tn, rf, ci = result2
    assert tn == 'test_table'
    assert rf == 'DYNAMIC'
    print(f"PASS: SDI extract from file: {tn} ({rf}), {len(ci)} columns")

    # 测试 filter
    result3 = SDIExtractor.extract_schema_from_file(tmp_sdi, 'nonexistent')
    assert result3 is None, "Should not match nonexistent table"
    print("PASS: SDI table filter test")

    os.unlink(tmp_sdi)

    print("\n" + "=" * 60)
    print("All tests passed!")
    print("=" * 60)


if __name__ == '__main__':
    run_tests()
