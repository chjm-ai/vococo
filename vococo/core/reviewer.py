"""评审子代理(code-reviewer):合并前用一个【干净上下文】看本分支改动,只挑问题、不动代码。

为什么单独做一个子代理,而不是让写代码的那个自己回头看(2026-10-01 多 Agent 调研结论):
- 写代码的上下文里全是它自己的思路,回头看容易「越看越对」;新上下文只拿 diff 和代码,
  没有先入为主。Cognition 2026 年的实测:这是多 Agent 里少数被证明有效的用法
  (写操作单线程,其他 Agent 只贡献判断),平均每个 PR 能多抓出约 2 个问题。
- 只读:工具只给 Read/Grep/Glob/Bash(Bash 用来跑 git diff / 测试),不给 Edit/Write;
  修不修、怎么修由主 Agent 决定,避免两个 Agent 同时改同一份代码。
  注意这是「约定只读」:Bash 理论上能写文件(sed -i 之类),靠提示词约束 + danger.py 兜底。
  所属 Agent 的 disallowed_tools 禁了 Bash 时评审员也拿不到(父会话的 disallowed_tools 会传给子代理,
  2026-10-01 真机实测),那时它只能用 Read/Grep/Glob 看代码。
- 模型跟随主会话(inherit):大小模型不对称搭配效果差,也省得第三方供应商下别名对不上。

只在「显式选了 Git 项目」的会话里挂(和 coding 技能组同一个判断),普通聊天看不到它。
"""
from __future__ import annotations

from claude_agent_sdk import AgentDefinition

from ..gateway import settings_store

NAME = "code-reviewer"

DESCRIPTION = (
    "代码评审员:用干净上下文评审当前分支相对主干的改动,只找真问题、不改代码。"
    "改完代码、合并/提交到主干之前主动调用一次;传入:这次改了什么、为什么改(一两句)。"
)

PROMPT = """你是一名资深代码评审员。你拿到的是别人刚写完、准备合并的改动,你没参与编写,要用新鲜的眼光挑错。

## 怎么做
1. 先弄清改动范围:`git merge-base HEAD main` 找分叉点,再 `git diff <分叉点>...HEAD --stat` 和完整 diff;
   有未提交改动也一起看(`git diff`、`git status --short`)。主干不叫 main 时用 `git symbolic-ref refs/remotes/origin/HEAD` 找。
2. 对每处改动,读改动文件的上下文和调用方(Read/Grep),确认它在真实调用链里怎么被用。
3. 重点找:逻辑错误、边界条件、异常路径没处理、并发/状态不一致、老数据兼容(数据库列、配置字段)、
   安全问题(注入、越权、路径穿越、密钥泄露)、前后端字段对不上、改了行为但没改的调用点、缺关键测试。
4. 每个怀疑都要自己验证:找到具体触发条件(什么输入/状态 → 什么错误结果)。验证不了就标「待验证」,不许编。
5. 需要时可以跑相关的单个测试确认(如 `uv run pytest path::test_name -q`),不要跑耗时很长的全量套件。

## 不要做
- 不修改任何文件,不提交,不切分支。
- 不提代码风格、命名、注释措辞这类偏好问题;不重复罗列改动做了什么。

## 输出(中文,越短越好)
按严重程度排序,每条一行:
`[确认|待验证] 文件:行号 — 问题一句话 — 触发场景一句话`
最后一行给结论:「可以合并」或「修完上面 N 条再合并」。没发现问题就只写「没发现需要修的问题,可以合并」。"""


def definitions(cwd: str | None, is_explicit_project: bool) -> dict[str, AgentDefinition] | None:
    """这一轮要注册的程序化子代理;不是显式 Git 项目会话 → None(不挂,选项和以前完全一样)。"""
    if not (is_explicit_project and settings_store.is_git_workspace(cwd)):
        return None
    return {
        NAME: AgentDefinition(
            description=DESCRIPTION,
            prompt=PROMPT,
            tools=["Read", "Grep", "Glob", "Bash"],
            disallowedTools=["Edit", "Write", "NotebookEdit"],
            model="inherit",
        )
    }
