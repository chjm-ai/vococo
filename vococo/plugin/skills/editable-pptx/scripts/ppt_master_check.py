"""核对共享技能与本次审核的完整上游源码一致；不安装、不更新。"""
import hashlib
import os
from pathlib import Path
import subprocess
import sys


UPSTREAM_COMMIT = "78759bf62e9a1ef43e926ff9e50afa4cfe9a63cb"
EXPECTED_DIGEST = "a3f7525001426b54af1dc553493e84b68d66129a84699c34171c06f1094aebfd"


def source_digest(root):
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError("共享技能内部不能包含软链接")
        if not path.is_file():
            continue
        name = path.relative_to(root).as_posix().encode()
        digest.update(name + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def verify(root):
    if not (root / "SKILL.md").is_file():
        raise ValueError(f"PPT Master 未安装：{root}")
    if source_digest(root) != EXPECTED_DIGEST:
        raise ValueError(f"PPT Master 与已核验提交 {UPSTREAM_COMMIT} 不一致；停止，请维护者复核更新")
    subprocess.run(
        [sys.executable, "-B", str(root / "scripts/attribution_guard.py")], check=True,
    )
    return root / "SKILL.md"


def main():
    root = Path(os.environ.get(
        "VOCOCO_PPT_MASTER_DIR", "~/Desktop/Repos/AI_Skills/ppt-master"
    )).expanduser().resolve()
    try:
        entry = verify(root)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(entry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
