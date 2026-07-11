"""Qwen-Omni-Realtime(阿里云百炼)WebRTC 信令代理——P3 阶段二,2026-07-10。

架构几经调整,记一下why,别重蹈覆辙:
- 阶段一(已废弃)搭过一个 WS 版的 OmniRealtimeSession,让 Omni 自己识别+生成回复+
  调用工具,前端走 WebRTC 音频轨道。真机测试暴露两个问题:①session.update 里的
  turn_detection.create_response 字段文档有、真连线测过不生效,关不掉 Omni 自己
  生成回复;②即便工具/画像都喂给它,Omni 终究是跟 Claude 完全独立的模型,没有
  Read/Grep/项目 AGENTS.md/记忆这些工具,答不出真实业务问题("看不到我的业务场景")。
- 现在的架构:Omni 只当"耳朵"——WebRTC 连线只用来做识别(ASR)+断句(VAD)+
  打断信号,session.update 的 modalities 只给 ["text"],不给 "audio",这样 Omni
  自己生成的回复(不能禁用那个)只有文字、没有语音,不会真的被听到,等于白生成、
  静默丢弃。识别到一整句话后,前端把文字转发给现有的 /voice/send(跟文字聊天、
  跟以前 ws.py 版本一样,带完整 PERSONA/USER.md/项目 AGENTS.md/记忆索引 + 全套
  原生工具 + 语音任务派发三件套),真正的回答由 Claude 生成,合成/播放复用前端
  已有的 playQueue/pumpPlayback(经过今天几轮真机修复,单路可靠,不再是今天早些
  时候那种自建 WebRTC 环回 hack)。见 web_static/index.html 的 sendOmniTurn/
  wireOmniDataChannel。

本文件现在只剩 WebRTC 的 SDP 信令代理这一件事——浏览器自己发不了这个请求
(阿里云文档原话:跨域限制),也不能拿到真实 DASHSCOPE_API_KEY,必须后端代理。
信令换完之后,识别音频走浏览器跟阿里云直连(WebRTC P2P/UDP),不经过我们的服务器。

热词(2026-07-10 查证,别再研究一遍):Omni-Realtime 的 session.update 和它的
转写模型 qwen3-asr-flash-realtime 都【不支持】热词/自定义词表/上下文增强——
阿里云文档明说热词仅 Fun-ASR/Paraformer 系列有,上下文增强仅 fun-asr-realtime。
误听("子代理"→"纸袋")的纠错只能靠 Claude 这个大脑做,见 prompts.py 的
【转写容错】规则;想上真热词得整条换 Fun-ASR 链路,不值得。
"""
from __future__ import annotations

import aiohttp

from .. import config

# WebRTC 的 SDP 信令交换端点——注意这不是 DashScope 其他接口用的全局域名,
# 必须是"{WorkspaceId}.cn-beijing.maas.aliyuncs.com"这种工作区专属域名,
# 2026-07-10 真机连线验证过全局域名对这个路径直接 404。
_WEBRTC_URL_TMPL = "https://{workspace}.cn-beijing.maas.aliyuncs.com/api/v1/webrtc/realtime"


async def exchange_webrtc_sdp(offer_sdp: str) -> str:
    """把浏览器生成的 SDP offer 转发给 Qwen-Omni-Realtime,换回 SDP answer。

    2026-07-10 用 aiortc 模拟浏览器连线验证过:ICE/DTLS 能完整握手到 connected,
    服务端会推一个 label="txt" 的 DataChannel 并主动发 session.created,音频
    m-line 也正确协商——这一步(信令代理本身)是可靠的,真机测的重点在别处。
    """
    if not config.VOICE_OMNI_WORKSPACE_ID:
        raise RuntimeError("未配置 VOICE_OMNI_WORKSPACE_ID(去百炼控制台复制业务空间ID)")
    url = (
        _WEBRTC_URL_TMPL.format(workspace=config.VOICE_OMNI_WORKSPACE_ID)
        + f"?model={config.VOICE_OMNI_REALTIME_MODEL}"
    )
    async with aiohttp.ClientSession() as sess:
        async with sess.post(
            url, data=offer_sdp,
            headers={
                "Authorization": f"Bearer {config.DASHSCOPE_API_KEY}",
                "Content-Type": "application/sdp",
            },
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"WebRTC 信令交换失败 status={resp.status} body={body[:300]!r}")
            return body
