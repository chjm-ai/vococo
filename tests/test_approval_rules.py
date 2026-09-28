"""永久审批规则 + 审批记录(tools/danger.py + memory/approvals.py)。"""
from __future__ import annotations

import anyio

from vococo.tools import danger
from vococo.tools.danger import pretool_guard_hook


class _FakeAdapter:
    def __init__(self):
        self.choice = None

    async def present_choice(self, chat_id, choice):
        self.choice = choice

    async def send(self, *a, **k):
        pass


def _click(label: str, tool_name: str, tool_input: dict, key: str, cwd=None, proot=None):
    """开一轮审批,点文字包含 label 的按钮;返回 (hook 输出, 弹窗对象;没弹窗则 None)。"""
    from vococo.gateway import clarify

    async def scenario():
        adapter = _FakeAdapter()
        tok = clarify.set_current(key, adapter, "chat")
        cwd_tok = danger.set_cwd(cwd, project_root=proot)
        out: dict = {}
        try:
            async with anyio.create_task_group() as tg:

                async def run():
                    out["v"] = await pretool_guard_hook(
                        {"tool_name": tool_name, "tool_input": tool_input}, None, {}
                    )

                tg.start_soon(run)
                for _ in range(200):
                    if adapter.choice is not None or "v" in out:
                        break
                    await anyio.sleep(0.005)
                if adapter.choice is not None:
                    cmd = next(c for c, lab in adapter.choice.options if label in lab)
                    clarify.resolve_button(cmd.split()[1], cmd.split()[2])
        finally:
            danger.reset_cwd(cwd_tok)
            clarify.reset_current(tok)
            clarify.clear_session(key)
            danger.clear_session_approvals(key)
        return out["v"], adapter.choice

    return anyio.run(scenario)


def test_rule_target_write_dir(isolated, monkeypatch):
    home = isolated / "home"
    (home / "notes").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    reason = "写工作目录外的文件(x)"
    rt = danger._rule_target("Write", {"file_path": str(home / "notes" / "a.md")}, reason, "/tmp")
    assert rt[0] == "write_dir" and rt[1].endswith("/notes")
    # 家目录本身、凭据目录、家目录外 → 不给规则
    assert danger._rule_target("Write", {"file_path": str(home / ".zshrc")}, reason, "/tmp") is None
    (home / ".ssh").mkdir()
    assert danger._rule_target("Write", {"file_path": str(home / ".ssh" / "config")}, reason, "/tmp") is None
    assert danger._rule_target("Write", {"file_path": "/etc/hosts"}, reason, "/tmp") is None


def test_rule_target_git_push(tmp_path):
    reason = "git push(推送到远端,对外)"
    tok = danger.set_cwd(str(tmp_path / "wt"), project_root=str(tmp_path))
    try:
        rt = danger._rule_target("Bash", {"command": "git push origin main"}, reason, str(tmp_path / "wt"))
        assert rt[0] == "git_push" and rt[1] == str(tmp_path.resolve())
        # 切目录 / 指定别的仓库 / 夹带装包 → 不给规则
        assert danger._rule_target("Bash", {"command": "cd /x && git push"}, reason, None) is None
        assert danger._rule_target("Bash", {"command": "git -C /x push"}, reason, None) is None
        assert danger._rule_target("Bash", {"command": "npm install x && git push"}, reason, None) is None
    finally:
        danger.reset_cwd(tok)


def test_rule_target_forbidden_kinds():
    assert danger._rule_target("Bash", {"command": "pip install x"}, "包安装(改动环境)", None) is None
    assert danger._rule_target("Bash", {"command": "kill 1"}, "进程终止命令", None) is None
    rt = danger._rule_target("mcp__lemlist_lite__send_email", {}, "外部写", None)
    assert rt == ("mcp_tool", "mcp__lemlist_lite__send_email", "以后调用 send_email 不再询问")


def test_forever_saves_rule_and_skips_next_prompt(isolated):
    from vococo.memory import approvals

    tool = "mcp__lemlist_lite__send_email"
    out, choice = _click("永远允许", tool, {"to": "a@b.c"}, "web:p1:c1")
    assert out == {}
    assert any("永远允许" in lab for _c, lab in choice.options)
    assert [r["scope"] for r in approvals.list_rules()] == [tool]

    # 第二次(换个会话):命中规则,不再弹窗
    out2, choice2 = _click("永远允许", tool, {"to": "x@y.z"}, "web:p1:c2")
    assert out2 == {} and choice2 is None
    assert approvals.list_rules()[0]["hits"] == 1

    decisions = [a["decision"] for a in approvals.list_audit()]
    assert decisions == ["rule", "approved_forever"]


def test_no_forever_option_when_not_rule_eligible(isolated):
    from vococo.memory import approvals

    out, choice = _click("拒绝", "Bash", {"command": "pip install x"}, "web:p1:c3")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert not any("永远允许" in lab for _c, lab in choice.options)
    assert approvals.list_audit()[0]["decision"] == "denied"


def test_task_session_shows_task_option(isolated):
    _out, choice = _click("拒绝", "Bash", {"command": "pip install x"}, "task:abcd1234")
    assert any("本轮任务都允许" in lab for _c, lab in choice.options)


def test_noninteractive_deny_is_audited(isolated):
    from vococo.memory import approvals

    out = anyio.run(lambda: pretool_guard_hook(
        {"tool_name": "Bash", "tool_input": {"command": "pip install x"}}, None, {}
    ))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert approvals.list_audit()[0]["decision"] == "noninteractive_deny"


def test_task_without_channel_goes_to_bell(isolated, monkeypatch):
    """后台任务但没有 Web 审批通道(纯 CLI/TUI)→ 直接以「已超时」进铃铛,记在任务会话名下。"""
    from vococo.gateway import clarify
    from vococo.memory import approvals, notices

    monkeypatch.setattr(clarify, "_bg_adapter", None)
    tok = danger.set_task_session("task:zz")
    try:
        out = anyio.run(lambda: pretool_guard_hook(
            {"tool_name": "Bash", "tool_input": {"command": "pip install x"}}, None, {}
        ))
    finally:
        danger.reset_task_session(tok)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    row = approvals.list_audit()[0]
    assert row["decision"] == "deferred" and row["session_key"] == "task:zz"
    n = notices.list_open()[0]
    assert n["session_key"] == "task:zz" and n["status"] == "expired" and n["kind"] == "approval"


def test_block_is_audited(isolated):
    from vococo.memory import approvals

    cmd = "mk" + "fs.ext4 /dev/sda1"  # 拆开写,免得被本机的命令拦截器误判
    out = anyio.run(lambda: pretool_guard_hook(
        {"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {}
    ))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert approvals.list_audit()[0]["decision"] == "blocked"


def test_delete_rule(isolated):
    from vococo.memory import approvals

    r = approvals.add_rule("mcp_tool", "mcp__x__y", "demo")
    assert approvals.delete_rule(r["id"]) is True
    assert approvals.list_rules() == []
