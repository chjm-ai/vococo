"""TG 竖线表格 → 分组列表:Telegram 无表格渲染,`format_tables` 把它改写成可读列表。"""
from __future__ import annotations

from vococo.gateway.adapters.telegram import format_tables


def test_simple_table_becomes_groups():
    md = (
        "看看:\n"
        "| 名称 | 价格 | 库存 |\n"
        "|------|------|------|\n"
        "| 苹果 | 3元 | 100 |\n"
        "| 香蕉 | 2元 | 50 |\n"
        "就这些"
    )
    out = format_tables(md)
    assert "|" not in out.replace("看看:", "")  # 表格竖线已消失
    assert "▸ 苹果" in out
    assert "• 价格：3元" in out
    assert "• 库存：100" in out
    assert "▸ 香蕉" in out
    assert out.startswith("看看:")   # 表格外文字原样保留
    assert out.endswith("就这些")


def test_row_label_table_uses_first_col_as_heading():
    # 首列比表头多一列 → 首列(项目)当小标题
    md = (
        "| 项目 | 状态 | 负责人 |\n"
        "|------|------|--------|\n"
        "| 登录 | 完成 | 张三 |\n"
        "| 支付 | 进行中 | 李四 |"
    )
    out = format_tables(md)
    assert "▸ 登录" in out
    assert "• 状态：完成" in out
    assert "• 负责人：张三" in out
    assert "▸ 支付" in out


def test_code_fence_table_untouched():
    md = "```\n| a | b |\n|---|---|\n| 1 | 2 |\n```"
    assert format_tables(md) == md  # 代码块内保持原样


def test_no_table_passthrough():
    assert format_tables("普通一句话,没有表格") == "普通一句话,没有表格"
    # 有竖线但不是表格(缺分隔行)也原样返回
    assert format_tables("a | b | c") == "a | b | c"


def test_idempotent():
    md = "| x | y |\n|---|---|\n| 1 | 2 |"
    once = format_tables(md)
    assert format_tables(once) == once  # 二次调用不再变化
