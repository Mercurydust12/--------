#!/usr/bin/env python3
"""
batch_rename.py
批量图片重命名工具（支持 dry-run、按序号/日期/原名/EXIF、递归、撤销）

用法示例见脚本末尾的注释或运行 `python batch_rename.py -h`
"""
import argparse
from pathlib import Path
from datetime import datetime
import json
import os
import re
import sys

# 尝试导入 Pillow（可选，用于读取 EXIF 时间）
try:
    from PIL import Image, ExifTags
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False

INVALID_CHARS_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

def sanitize_filename(name: str) -> str:
    # 移除或替换文件名中在 Windows 等系统中非法的字符
    name = INVALID_CHARS_RE.sub('_', name)
    # 去除前后空格
    name = name.strip()
    # 如果名字变成空串，返回下划线占位
    return name or "_"

def get_files(directory: Path, exts, recursive):
    if recursive:
        it = directory.rglob('*')
    else:
        it = directory.glob('*')
    for p in sorted(it):
        if p.is_file():
            if not exts:
                yield p
            else:
                if p.suffix.lower().lstrip('.') in exts:
                    yield p

def get_exif_datetime(path: Path):
    if not PIL_AVAILABLE:
        return None
    try:
        img = Image.open(path)
        exif = img._getexif()
        if not exif:
            return None
        # 找 DateTimeOriginal 对应的 tag id
        tag_map = {v:k for k,v in ExifTags.TAGS.items()}
        for candidate in ("DateTimeOriginal","DateTime"):
            tagid = tag_map.get(candidate)
            if tagid and tagid in exif:
                val = exif[tagid]
                # 常见格式 "YYYY:MM:DD hh:mm:ss"
                try:
                    dt = datetime.strptime(val, "%Y:%m:%d %H:%M:%S")
                    return dt
                except Exception:
                    pass
        return None
    except Exception:
        return None

def unique_target(path: Path):
    """若目标已存在，加上后缀 _1/_2 ... 保证唯一"""
    if not path.exists():
        return path
    parent = path.parent
    stem = path.stem
    suffix = path.suffix
    i = 1
    while True:
        candidate = parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1

def main():
    parser = argparse.ArgumentParser(description="批量图片重命名工具（dry-run 默认，-a 才会真正改名）")
    parser.add_argument('--dir', '-d', type=Path, default=Path('.'), help='目标目录（默认当前目录）')
    parser.add_argument('--pattern', '-p', type=str, default='{prefix}{counter}',
                        help=("重命名模式，支持占位符：{counter},{orig},{ext},{date},{mtime}。"
                              "例如 'IMG_{counter:04d}' 或 '{date:%%Y%%m%%d}_{orig}'。"))
    parser.add_argument('--prefix', type=str, default='', help='若未在 pattern 中指定 prefix，可用此项')
    parser.add_argument('--suffix', type=str, default='', help='若未在 pattern 中指定 suffix，可用此项')
    parser.add_argument('--start', type=int, default=1, help='序号起始值（默认1）')
    parser.add_argument('--digits', type=int, default=0, help='若 pattern 中使用裸 {counter}，可通过此参数补零位数（例如 4）')
    parser.add_argument('--ext', type=str, default='', help='只处理这些扩展名（逗号分隔，不含点），例如 "jpg,png"。默认所有文件')
    parser.add_argument('--recursive', '-r', action='store_true', help='递归子目录')
    parser.add_argument('--sort-by', type=str, choices=['name','mtime','ctime'], default='name', help='文件排序方式')
    parser.add_argument('--exif-date', action='store_true', help='若可用，优先使用图片 EXIF 的 DateTimeOriginal 作为 {date}')
    parser.add_argument('--apply', '-a', action='store_true', help='实际执行重命名（默认仅预览 dry-run）')
    parser.add_argument('--map-file', type=Path, default=None, help='保存重命名映射的文件（JSON）。默认会在 --dir 下生成 rename_map_TIMESTAMP.json')
    parser.add_argument('--undo', type=Path, default=None, help='提供之前保存的映射文件以撤销重命名')
    parser.add_argument('--verbose', '-v', action='store_true', help='详细输出')
    args = parser.parse_args()

    if args.undo:
        # 撤销模式
        mapf = args.undo
        if not mapf.exists():
            print(f"映射文件不存在: {mapf}")
            sys.exit(1)
        with open(mapf, 'r', encoding='utf-8') as f:
            data = json.load(f)
        # data 应该是 [{ "src": "...", "dst": "..." }, ...]
        reversed_ops = []
        for item in data:
            src = Path(item['src'])
            dst = Path(item['dst'])
            # 如果 dst 存在且 src 不存在（安全撤回）
            if dst.exists() and not src.exists():
                try:
                    dst.rename(src)
                    reversed_ops.append((str(dst), str(src)))
                    if args.verbose:
                        print(f"REVERT: {dst} -> {src}")
                except Exception as e:
                    print(f"撤销失败: {dst} -> {src} : {e}")
            else:
                print(f"跳过（条件不满足）：{dst} -> {src}")
        print(f"撤销完成，成功 {len(reversed_ops)} 项。")
        sys.exit(0)

    if args.ext:
        exts = set([e.strip().lower() for e in args.ext.split(',') if e.strip()])
    else:
        exts = set()

    files = list(get_files(args.dir, exts, args.recursive))
    if not files:
        print("未发现任何匹配的文件。")
        sys.exit(0)

    # 排序
    if args.sort_by == 'mtime':
        files.sort(key=lambda p: p.stat().st_mtime)
    elif args.sort_by == 'ctime':
        files.sort(key=lambda p: p.stat().st_ctime)
    else:
        files.sort(key=lambda p: p.name)

    pattern = args.pattern
    # 若用户指定了 digits 且 pattern 有裸 {counter}，将其替换为具有零填充格式
    if args.digits and re.search(r'\{counter\}', pattern):
        pattern = re.sub(r'\{counter\}', f'{{counter:0{args.digits}d}}', pattern)

    counter = args.start
    ops = []
    timestamp_str = datetime.now().strftime('%Y%m%dT%H%M%S')
    map_file = args.map_file or (args.dir / f"rename_map_{timestamp_str}.json")
    for p in files:
        try:
            orig_stem = p.stem
            ext = p.suffix.lstrip('.')
            # 选择日期来源
            exif_dt = get_exif_datetime(p) if args.exif_date else None
            mtime_dt = datetime.fromtimestamp(p.stat().st_mtime)
            date_dt = exif_dt or mtime_dt

            mapping = {
                'counter': counter,
                'orig': orig_stem,
                'ext': ext,
                'date': date_dt,
                'mtime': mtime_dt,
                'prefix': args.prefix,
                'suffix': args.suffix
            }

            try:
                new_name_base = pattern.format_map(mapping)
            except KeyError as e:
                print(f"Pattern 格式化错误，未知占位符: {e}. pattern={pattern}")
                sys.exit(1)
            except Exception as e:
                print(f"Pattern 格式化失败: {e}")
                sys.exit(1)

            # 如果 pattern 包含 {ext}，就认为用户已经指定了扩展名；否则默认保留原扩展
            if '{ext' in pattern:
                new_name = sanitize_filename(new_name_base)
            else:
                new_name = sanitize_filename(new_name_base) + ('.' + ext if ext else '')

            target = p.parent / new_name
            target = unique_target(target)

            ops.append({'src': str(p), 'dst': str(target)})
            if args.verbose or not args.apply:
                print(f"{p.name}  ->  {target.name}")

            if args.apply:
                try:
                    p.rename(target)
                except Exception as e:
                    print(f"重命名失败: {p} -> {target} : {e}")
            counter += 1

        except Exception as e:
            print(f"处理文件失败 {p}: {e}")

    # 保存映射
    if args.apply:
        try:
            with open(map_file, 'w', encoding='utf-8') as f:
                json.dump(ops, f, ensure_ascii=False, indent=2)
            print(f"\n已将重命名映射保存到: {map_file}")
        except Exception as e:
            print(f"保存映射文件失败: {e}")
    else:
        print("\n（这是预览 dry-run，未执行实际重命名。加上 -a/--apply 才会真正改名）")
        print(f"将要重命名的文件数量: {len(ops)}")

if __name__ == '__main__':
    main()
