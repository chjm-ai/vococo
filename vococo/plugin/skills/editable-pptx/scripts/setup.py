"""一次性安装固定版本的 PPT 依赖；生成过程不联网、不自动装包。"""
from pathlib import Path
import os
import shutil
import subprocess


SKILL = Path(__file__).resolve().parent.parent


def main():
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("需要先安装 Node.js（含 npm）。")
    root = Path(os.environ.get("VOCOCO_PPTX_HOME", Path.home() / ".cache/vococo-pptx/4.0.1"))
    root.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "package-lock.json"):
        source, target = SKILL / name, root / name
        data = source.read_bytes()
        if target.exists() and target.read_bytes() != data:
            raise SystemExit(f"依赖清单不一致，未覆盖：{target}")
        target.write_bytes(data)
    subprocess.run([npm, "ci", "--prefix", str(root), "--ignore-scripts", "--no-audit", "--no-fund"], check=True)
    print(f"PPT 依赖已安装：{root}")


if __name__ == "__main__":
    main()
