#!/usr/bin/env python3
"""视频素材库管理脚本

用法:
  manage.py add <url> [--tags t1,t2] [--source official|youtube|pexels|pixabay|original] [--name custom-name] [--notes "描述"]
  manage.py batch <file>            # file 每行一个 URL，可带 tab 分隔的 tags
  manage.py search [--tag x] [--source x] [--min-dur 秒] [--max-dur 秒] [--keyword x]
  manage.py list [--summary]
  manage.py remove <id>
  manage.py info <id>
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

LIBRARY = Path.home() / "Media" / "video-assets"
INDEX = LIBRARY / "index.json"
CLIPS = LIBRARY / "clips"


def load_index():
    if INDEX.exists():
        return json.loads(INDEX.read_text())
    return []


def save_index(data):
    INDEX.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def next_id(entries):
    if not entries:
        return "v0001"
    nums = [int(e["id"][1:]) for e in entries if e["id"].startswith("v")]
    return f"v{max(nums) + 1:04d}"


def probe(filepath):
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", str(filepath),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    d = json.loads(result.stdout)
    fmt = d["format"]
    vs = [s for s in d["streams"] if s["codec_type"] == "video"]
    v = vs[0] if vs else {}
    r = v.get("r_frame_rate", "0/1")
    num, den = r.split("/")
    fps = round(int(num) / int(den)) if int(den) else 0
    return {
        "duration": round(float(fmt.get("duration", 0)), 1),
        "resolution": f"{v.get('width', '?')}x{v.get('height', '?')}",
        "fps": fps,
        "size_mb": round(int(fmt.get("size", 0)) / 1048576, 1),
    }


def sanitize_filename(name):
    return re.sub(r"[^\w\-.]", "-", name).strip("-")


def download(url, output_name=None):
    CLIPS.mkdir(parents=True, exist_ok=True)
    if output_name:
        template = str(CLIPS / f"{sanitize_filename(output_name)}.%(ext)s")
    else:
        template = str(CLIPS / "%(title)s.%(ext)s")

    cmd = [
        "yt-dlp",
        "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", template,
        "--no-playlist",
        "--no-overwrites",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout + result.stderr

    # 从输出找到最终文件路径
    for line in output.split("\n"):
        if "Merging formats into" in line:
            m = re.search(r'"(.+?)"', line)
            if m:
                return Path(m.group(1))
        if "[download] Destination:" in line:
            path = line.split("Destination:", 1)[1].strip()
            if path.endswith(".mp4"):
                return Path(path)
        if "has already been downloaded" in line:
            m = re.search(r"\[download\] (.+?) has already", line)
            if m:
                return Path(m.group(1))

    if result.returncode != 0:
        print(f"下载失败: {url}", file=sys.stderr)
        print(output, file=sys.stderr)
        return None

    # fallback: 找最新的 mp4
    mp4s = sorted(CLIPS.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    return mp4s[0] if mp4s else None


def cmd_add(args):
    entries = load_index()

    # 去重检查
    for e in entries:
        if e.get("source_url") == args.url:
            print(f"已存在: {e['id']} — {e['file']}")
            return

    filepath = download(args.url, args.name)
    if not filepath:
        sys.exit(1)

    meta = probe(filepath)
    rel = filepath.relative_to(LIBRARY)

    entry = {
        "id": next_id(entries),
        "file": str(rel),
        "source_url": args.url,
        "source": args.source or "youtube",
        "copyright": "brand-promo" if args.source == "official" else "fair-use",
        "tags": [t.strip() for t in args.tags.split(",")] if args.tags else [],
        "duration": meta["duration"],
        "resolution": meta["resolution"],
        "fps": meta["fps"],
        "size_mb": meta["size_mb"],
        "added": subprocess.run(["date", "+%Y-%m-%d"], capture_output=True, text=True).stdout.strip(),
        "notes": args.notes or "",
    }
    entries.append(entry)
    save_index(entries)
    print(f"已入库: {entry['id']} — {rel} ({meta['duration']:.0f}s, {meta['resolution']}, {meta['size_mb']}MB)")


def cmd_batch(args):
    lines = Path(args.file).read_text().strip().split("\n")
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        url = parts[0].strip()
        tags = parts[1].strip() if len(parts) > 1 else ""
        notes = parts[2].strip() if len(parts) > 2 else ""

        fake_args = argparse.Namespace(
            url=url, tags=tags, source=args.source, name=None, notes=notes,
        )
        cmd_add(fake_args)


def cmd_search(args):
    entries = load_index()
    results = []
    for e in entries:
        if args.tag and args.tag not in e.get("tags", []):
            continue
        if args.source and e.get("source") != args.source:
            continue
        if args.min_dur and e.get("duration", 0) < args.min_dur:
            continue
        if args.max_dur and e.get("duration", 0) > args.max_dur:
            continue
        if args.keyword:
            kw = args.keyword.lower()
            haystack = " ".join(e.get("tags", [])) + " " + e.get("notes", "") + " " + e.get("file", "")
            if kw not in haystack.lower():
                continue
        results.append(e)

    if not results:
        print("无匹配素材")
        return

    for e in results:
        dur = f"{e['duration']:.0f}s"
        print(f"  {e['id']}  {dur:>6s}  {e['resolution']:>10s}  {e['file']}")
        if e.get("notes"):
            print(f"          {e['notes']}")
        print(f"          tags: {', '.join(e.get('tags', []))}")
    print(f"\n共 {len(results)} 条")


def cmd_list(args):
    entries = load_index()
    if not entries:
        print("素材库为空")
        return

    if args.summary:
        total_dur = sum(e.get("duration", 0) for e in entries)
        total_size = sum(e.get("size_mb", 0) for e in entries)
        sources = {}
        for e in entries:
            s = e.get("source", "unknown")
            sources[s] = sources.get(s, 0) + 1
        print(f"素材总数: {len(entries)}")
        print(f"总时长: {total_dur / 60:.1f} 分钟")
        print(f"总大小: {total_size:.0f} MB")
        print(f"来源分布: {', '.join(f'{k}({v})' for k, v in sources.items())}")
        # tag 统计
        tag_counts = {}
        for e in entries:
            for t in e.get("tags", []):
                tag_counts[t] = tag_counts.get(t, 0) + 1
        top_tags = sorted(tag_counts.items(), key=lambda x: -x[1])[:10]
        print(f"热门标签: {', '.join(f'{t}({c})' for t, c in top_tags)}")
        return

    for e in entries:
        dur = f"{e['duration']:.0f}s"
        print(f"  {e['id']}  {dur:>6s}  {e['resolution']:>10s}  {e.get('size_mb', 0):.0f}MB  {e['file']}")


def cmd_remove(args):
    entries = load_index()
    found = None
    for i, e in enumerate(entries):
        if e["id"] == args.id:
            found = i
            break
    if found is None:
        print(f"未找到 {args.id}")
        sys.exit(1)

    entry = entries.pop(found)
    filepath = LIBRARY / entry["file"]
    if filepath.exists():
        filepath.unlink()
        print(f"已删除文件: {entry['file']}")
    save_index(entries)
    print(f"已从索引移除: {entry['id']}")


def cmd_info(args):
    entries = load_index()
    for e in entries:
        if e["id"] == args.id:
            print(json.dumps(e, ensure_ascii=False, indent=2))
            return
    print(f"未找到 {args.id}")


def main():
    parser = argparse.ArgumentParser(description="视频素材库管理")
    sub = parser.add_subparsers(dest="cmd")

    p_add = sub.add_parser("add")
    p_add.add_argument("url")
    p_add.add_argument("--tags", default="")
    p_add.add_argument("--source", default="youtube")
    p_add.add_argument("--name", default=None)
    p_add.add_argument("--notes", default="")

    p_batch = sub.add_parser("batch")
    p_batch.add_argument("file")
    p_batch.add_argument("--source", default="youtube")

    p_search = sub.add_parser("search")
    p_search.add_argument("--tag", default=None)
    p_search.add_argument("--source", default=None)
    p_search.add_argument("--min-dur", type=float, default=None)
    p_search.add_argument("--max-dur", type=float, default=None)
    p_search.add_argument("--keyword", default=None)

    p_list = sub.add_parser("list")
    p_list.add_argument("--summary", action="store_true")

    p_rm = sub.add_parser("remove")
    p_rm.add_argument("id")

    p_info = sub.add_parser("info")
    p_info.add_argument("id")

    args = parser.parse_args()
    if args.cmd == "add":
        cmd_add(args)
    elif args.cmd == "batch":
        cmd_batch(args)
    elif args.cmd == "search":
        cmd_search(args)
    elif args.cmd == "list":
        cmd_list(args)
    elif args.cmd == "remove":
        cmd_remove(args)
    elif args.cmd == "info":
        cmd_info(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
