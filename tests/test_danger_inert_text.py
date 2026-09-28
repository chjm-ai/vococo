"""惰性文本剥离(tools/danger.py _strip_inert_text):提交信息、写文件的 heredoc 正文
不参与危险判定;会被执行的文本(命令替换、交给解释器/管道给 shell)照旧检查。"""
from __future__ import annotations

from vococo.tools import danger

PUSH = "git " + "push"
PIP = "pip " + "install requests"
MKFS = "mk" + "fs.ext4 /dev/sda1"


def _verdict(cmd: str) -> str:
    return danger.classify("Bash", {"command": cmd})[0]


# ── 不该拦(以前误拦)────────────────────────────────────────────────────
def test_commit_message_text_is_ignored():
    assert _verdict(f'git commit -m "feat: 后台 {PUSH} 审批" -m "说明 {PIP}"') == "allow"
    assert _verdict(f"git add -A && git commit -q -m '修 {PUSH} 误判'") == "allow"
    assert _verdict(f'git commit --message="{MKFS} 只是文字"') == "allow"


def test_cat_heredoc_body_is_ignored():
    cmd = f"cat > /tmp/notes.md <<'EOF'\n先 {PIP}\n再 {PUSH}\n{MKFS}\nEOF\necho done"
    assert _verdict(cmd) == "allow"
    cmd2 = f"cat <<EOF >> /tmp/a.txt\n纯文字 {PIP}\nEOF"
    assert _verdict(cmd2) == "allow"


def test_commit_via_heredoc_is_ignored():
    cmd = f"git commit -F - <<'MSG'\nfix: {PUSH} 文案\nMSG"
    assert _verdict(cmd) == "allow"


# ── 该拦的照样拦 ──────────────────────────────────────────────────────────
def test_real_commands_outside_text_still_caught():
    assert _verdict(f'git commit -m "x" && {PUSH}') == "escalate"
    assert _verdict(f"cat > /tmp/a <<'EOF'\nhi\nEOF\n{PIP}") == "escalate"
    assert _verdict(MKFS) == "block"


def test_command_substitution_in_message_is_not_stripped():
    # 双引号里的 $(...) 会被 shell 先执行,不是纯数据
    assert _verdict(f'git commit -m "$({PIP})"') == "escalate"
    assert _verdict(f'git commit -m "`{MKFS}`"') == "block"


def test_unquoted_heredoc_with_substitution_is_checked():
    cmd = f"cat > /tmp/a <<EOF\n$({PIP})\nEOF"
    assert _verdict(cmd) == "escalate"


def test_heredoc_to_interpreter_is_checked():
    assert _verdict(f"python3 - <<'EOF'\nimport os\nos.system('{PIP}')\nEOF") == "escalate"
    assert _verdict(f"bash <<'EOF'\n{MKFS}\nEOF") == "block"


def test_heredoc_piped_to_shell_is_checked():
    assert _verdict(f"cat <<'EOF' | sh\n{PIP}\nEOF") == "escalate"


def test_written_script_then_executed_is_checked():
    cmd = f"cat > /tmp/x.sh <<'EOF'\n{PIP}\nEOF\nbash /tmp/x.sh"
    assert _verdict(cmd) == "escalate"


def test_unterminated_heredoc_kept():
    assert _verdict(f"cat > /tmp/a <<'EOF'\n{PIP}") == "escalate"


# ── ⑤「本轮任务都允许」只管当前一轮 ─────────────────────────────────────────
def test_task_round_approval_cleared_on_next_round():
    key = "task:round1"
    danger._mark_session_approved(key, "包安装(改动环境)")
    assert danger._is_session_approved(key, "包安装(改动环境)")
    tok = danger.set_task_session(key)  # 下一轮开头
    danger.reset_task_session(tok)
    assert not danger._is_session_approved(key, "包安装(改动环境)")
