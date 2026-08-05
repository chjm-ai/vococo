"""按租户解析数据路径——「按租户换路径」的唯一出口。

personal 模式全部回落到 config 里现有的全局路径(行为与引入本层之前完全一致);
server 模式落到 data/tenants/<tid>/ 下,租户之间物理隔离(删租户 = 删目录)。

server 模式的租户目录布局:
    data/tenants/<tid>/
      state.db        会话库(turns/session_meta/projects/user_prefs)
      brain/          长期记忆根(对应 personal 的 ~/AI_BRAIN:
                      内含 USER.md / MEMORY.md / memory/<topic>.md,布局完全镜像)
      workspace/      租户沙箱:agent 的 cwd,危险分级「写 cwd 外」的判定基准
      settings.json   该租户的运行时覆盖层(对应 personal 的 web_settings.json)
      images/ audio/  消息附件落盘
"""
from __future__ import annotations

from pathlib import Path

from .. import config
from . import context


def data_dir() -> Path:
    """当前租户的数据根:personal=仓库 data/(老数据零迁移);server=data/tenants/<tid>/。"""
    if not config.IS_SERVER:
        return config.DATA_DIR
    return config.TENANTS_DIR / context.current()


def brain_dir() -> Path:
    """长期记忆根:personal=~/AI_BRAIN;server=租户目录下 brain/(布局镜像 AI_BRAIN)。"""
    if not config.IS_SERVER:
        return config.AI_BRAIN_DIR
    return data_dir() / "brain"


def workspace_dir() -> Path | None:
    """agent 工作目录:personal=None(维持 worktree/项目体系,由 config.project_cwd_for 管);
    server=租户沙箱目录(调用方负责 mkdir)。"""
    if not config.IS_SERVER:
        return None
    return data_dir() / "workspace"


def settings_path() -> Path:
    """运行时设置 JSON:personal=data/web_settings.json;server=每租户一份。"""
    if not config.IS_SERVER:
        return config.DATA_DIR / "web_settings.json"
    return data_dir() / "settings.json"


def images_dir() -> Path:
    if not config.IS_SERVER:
        return config.IMAGES_DIR
    return data_dir() / "images"


def audio_dir() -> Path:
    if not config.IS_SERVER:
        return config.AUDIO_DIR
    return data_dir() / "audio"
