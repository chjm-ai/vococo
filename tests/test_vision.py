"""core/vision 图片转文字:判定 + 批量转换 + 失败兜底。

不真调百炼(测试不连网),monkeypatch 掉 _describe_one,只验证判定逻辑、
并发拼接格式和错误传播;真机调用链在 2026-08-03 用真实 key 验证过。
"""
from __future__ import annotations

import pytest

from claude_hermes.core import vision
from claude_hermes.core.agent import ImageAttachment


def _img() -> ImageAttachment:
    return ImageAttachment(data="QUJD", media_type="image/png")


def test_is_vision_capable():
    """官方订阅(env 空)直传;第三方默认转文字;命中名单直传。"""
    assert vision.is_vision_capable({}) is True
    assert vision.is_vision_capable({"ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic"}) is False
    # 视觉名单内的 host 直传(名单当前为空,规则本身要可测)
    assert vision.is_vision_capable({"ANTHROPIC_BASE_URL": "https://EXAMPLE.com/anthropic"}) is False
    # 大小写与多余路径不影响判定
    assert vision.is_vision_capable({"ANTHROPIC_BASE_URL": "HTTPS://API.DEEPSEEK.COM/anthropic"}) is False


@pytest.mark.anyio
async def test_convert_success(monkeypatch):
    """多张图并发成功 → 拼接文本带「图片 N」序号。"""
    async def fake(img):
        return "这是一张菜单截图", ""
    monkeypatch.setattr(vision, "_describe_one", fake)
    text, err = await vision.convert_images([_img(), _img()])
    assert err == ""
    assert text == "[图片附件: 图片1]\n这是一张菜单截图\n\n[图片附件: 图片2]\n这是一张菜单截图"


@pytest.mark.anyio
async def test_convert_partial_fail(monkeypatch):
    """任一张失败 → 整体失败,不产出残缺描述。"""
    async def fake(img):
        return None, "图片识别服务连接失败"
    monkeypatch.setattr(vision, "_describe_one", fake)
    text, err = await vision.convert_images([_img()])
    assert text is None
    assert "图片 1 读取失败" in err


@pytest.mark.anyio
async def test_convert_missing_key(monkeypatch):
    """未配 DASHSCOPE_API_KEY → 明确提示,不抛裸异常。"""
    monkeypatch.setattr(vision.config, "DASHSCOPE_API_KEY", "")
    text, err = await vision.convert_images([_img()])
    assert text is None
    assert "未配置图片识别" in err


@pytest.mark.anyio
async def test_convert_exception_propagates(monkeypatch):
    """_describe_one 内部炸了 → 转成可读错误,不吞异常。"""
    async def fake(img):
        raise RuntimeError("boom")
    monkeypatch.setattr(vision, "_describe_one", fake)
    text, err = await vision.convert_images([_img()])
    assert text is None
    assert "boom" in err
