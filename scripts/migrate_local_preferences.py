#!/usr/bin/env python3
"""从改动前的 Git 提交提取本机服务和 Upwork 配置，备份后仅追加缺失字段。"""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def source(ref: str, path: str) -> str:
    return subprocess.run(["git", "show", f"{ref}:{path}"], cwd=ROOT, check=True,
                          capture_output=True, text=True).stdout


def assignment(text: str, name: str):
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node.value
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return node.value
    raise ValueError(f"旧版本缺少 {name}，拒绝猜测迁移")


def home_path(node) -> Path:
    # 只识别原来的 Path.home() / 字面量，不执行任意旧代码。
    if isinstance(node, ast.Call) and ast.unparse(node) == "Path.home()":
        return Path.home()
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        right = ast.literal_eval(node.right)
        if isinstance(right, str):
            return home_path(node.left) / right
    raise ValueError("旧路径表达式变化，须人工核对")


def extract(ref: str) -> dict:
    tasks = source(ref, "vococo/cron/system_tasks.py")
    proxy = source(ref, "vococo/core/proxy_health.py")
    upwork = source(ref, "scripts/upwork-scraper.py")
    service = assignment(proxy, "SERVICE")
    if (not isinstance(service, ast.JoinedStr) or len(service.values) != 3
            or not isinstance(service.values[0], ast.Constant) or service.values[0].value != "gui/"
            or not isinstance(service.values[1], ast.FormattedValue)
            or ast.unparse(service.values[1].value) != "os.getuid()"
            or not isinstance(service.values[2], ast.Constant)
            or not service.values[2].value.startswith("/")):
        raise ValueError("旧服务标签表达式变化，须人工核对")
    return {
        "local_services": {
            "display_names": ast.literal_eval(assignment(tasks, "_DISPLAY_NAMES")),
            "proxy_launchd_label": service.values[2].value[1:],
        },
        "upwork": {
            "cdp_port": ast.literal_eval(assignment(upwork, "CDP_PORT")),
            "chrome_bin": ast.literal_eval(assignment(upwork, "CHROME_BIN")),
            "chrome_profile": str(home_path(assignment(upwork, "CHROME_PROFILE"))),
            "output_dir": str(home_path(assignment(upwork, "OUTPUT_DIR"))),
            "search_queries": ast.literal_eval(assignment(upwork, "SEARCH_QUERIES")),
            "match_keywords": ast.literal_eval(assignment(upwork, "MATCH_KEYWORDS")),
        },
    }


def merged_config(path: Path, additions: dict) -> tuple[bytes, dict]:
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("配置须为不含软链接的绝对路径")
    with path.open("rb") as stream:
        original = stream.read(1_024_001)
    if len(original) > 1_024_000:
        raise ValueError("实例配置过大")
    raw = original.decode("utf-8")
    if len(raw) > 256_000:
        raise ValueError("实例配置超过 256000 字符")
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("实例配置必须是 version=1 的对象")
    for name, fields in additions.items():
        current = data.setdefault(name, {})
        if not isinstance(current, dict):
            raise ValueError(f"{name} 必须是对象")
        for key, value in fields.items():
            if key in current and current[key] != value:
                raise ValueError(f"{name}.{key} 已有不同配置，拒绝覆盖")
            current[key] = value
    return original, data


def append_config(path: Path, additions: dict, *, apply: bool = False) -> Path | None:
    original, data = merged_config(path, additions)
    if not apply or json.loads(original) == data:
        return None
    encoded = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(encoded.decode("utf-8")) > 256_000:
        raise ValueError("追加后的配置过大")
    fd, backup_name = tempfile.mkstemp(prefix=path.name + ".backup-", suffix=".json", dir=path.parent)
    with os.fdopen(fd, "wb") as stream:
        stream.write(original)
        stream.flush()
        os.fsync(stream.fileno())
    fd, temporary = tempfile.mkstemp(prefix=".instance-config-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink() or path.read_bytes() != original:
            raise ValueError("配置在迁移期间变化，未覆盖，请重新核对")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return Path(backup_name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="实际追加；默认只检查")
    args = parser.parse_args()
    backup = append_config(args.output, extract(args.source_ref), apply=args.apply)
    if backup:
        print(f"已追加本机服务与 Upwork 配置；原文件备份：{backup}")
    else:
        print("配置检查通过，未写文件（只检查或内容已一致）")


if __name__ == "__main__":
    main()
