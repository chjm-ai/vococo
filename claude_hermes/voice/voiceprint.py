"""声纹识别:区分"这是本人在说话"还是"背景里别人在说话"。

背景(详见 docs/design/voice-companion/03-phase2-实现记录.md"声纹识别"一节):
免提场景麦克风常开,背景有人说话时,普通降噪(noiseSuppression)分不出"哪个
人声是你"——降噪只区分"人声 vs 非人声噪音",两个人说话在声学上是同一类信号。
真正对症的做法是"目标说话人识别"(speaker verification):给每句话提取一个
"声纹向量",跟本人的声纹参照比对,判断像不像是同一个人。

模型:resemblyzer 项目的开源预训练权重(Apache 2.0,3 层 LSTM + 线性投影,
256 维声纹,Google GE2E d-vector 思路)。resemblyzer 本身是 PyTorch 模型,
但生产这台 x86_64 macOS + Python 3.13 装不上 torch(新版 PyTorch 已不发
Intel Mac 的 wheel),所以离线转换成了 ONNX——转换脚本、数值校验(PyTorch
vs ONNX 输出最大误差 ~1e-6)记录见实现文档,模型文件在 voice/models/。
生产环境只需要 onnxruntime + librosa(算 mel 频谱),不需要 torch。

设计:异步、不卡对话速度(见实现文档"方案 B")——转写完成后立刻正常起
一轮回复,声纹比对在后台并行跑;比对结果如果"不像是你",事后把这一轮撤回
(见 voice/ws.py 的 _voiceprint_gate)。第一次用没有声纹参照,前几句话不
拦截、只用来建立参照(冷启动);声纹持久化跨会话保存,不用每次都重新学。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import librosa
import numpy as np
import onnxruntime as ort

from .. import config

_MODEL_PATH = Path(__file__).resolve().parent / "models" / "voiceprint_encoder.onnx"
_SAMPLE_RATE = 16000
_N_MELS = 40
_MEL_WINDOW_MS = 25
_MEL_STEP_MS = 10
_TARGET_DBFS = -30.0
_MIN_SAMPLES_FOR_EMBEDDING = _SAMPLE_RATE // 2  # 少于 0.5 秒的音频提不出可靠声纹

# 声纹参照最多保留最近这么多条样本,跟质心比对——不是无限增长的滑动平均。
# 只留最近的:防止早期一两条脏样本被后续样本稀释后还一直占着权重,直接用
# "最近 N 条"比"无限平均"更容易把偶发的污染样本挤出去。
_PROFILE_MAX_SAMPLES = 20

_session: ort.InferenceSession | None = None


def _get_session() -> ort.InferenceSession:
    global _session
    if _session is None:
        _session = ort.InferenceSession(str(_MODEL_PATH), providers=["CPUExecutionProvider"])
    return _session


def _normalize_volume(wav: np.ndarray, target_dbfs: float = _TARGET_DBFS) -> np.ndarray:
    """RMS 音量归一化,只提升不降低(跟 resemblyzer 训练时的预处理一致,
    见 resemblyzer.audio.normalize_volume 的 increase_only 用法)。"""
    rms = np.sqrt(np.mean(wav.astype(np.float64) ** 2)) + 1e-9
    current_dbfs = 20 * np.log10(rms)
    change = target_dbfs - current_dbfs
    if change < 0:
        return wav
    return (wav * (10 ** (change / 20))).astype(np.float32)


def pcm16_to_float(pcm_bytes: bytes) -> np.ndarray:
    """int16 PCM 字节(客户端麦克风原始格式)-> [-1, 1] 的 float32 数组。"""
    return np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0


def extract_embedding(pcm_bytes: bytes) -> np.ndarray | None:
    """一段 16kHz 单声道 PCM -> 256 维声纹向量(已 L2 归一化)。

    音频太短(<0.5秒)提不出可靠声纹,返回 None——调用方应当既不拿它去比对、
    也不纳入声纹参照,当作"这次没法判断"处理,不是"判断为不匹配"。
    """
    wav = pcm16_to_float(pcm_bytes)
    if len(wav) < _MIN_SAMPLES_FOR_EMBEDDING:
        return None
    wav = _normalize_volume(wav)
    mel = librosa.feature.melspectrogram(
        y=wav, sr=_SAMPLE_RATE,
        n_fft=int(_SAMPLE_RATE * _MEL_WINDOW_MS / 1000),
        hop_length=int(_SAMPLE_RATE * _MEL_STEP_MS / 1000),
        n_mels=_N_MELS,
    ).astype(np.float32).T
    if mel.shape[0] < 2:
        return None
    sess = _get_session()
    out = sess.run(None, {"mels": mel[None, :, :]})[0]
    return out[0]


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


@dataclass
class VoiceProfile:
    """声纹参照:最近若干条确认样本,跟质心比对(不是单一固定的"注册声纹")。"""

    embeddings: list = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.embeddings)


def _profile_path() -> Path:
    return config.DATA_DIR / "voice" / "voiceprint.npy"


def load_profile() -> VoiceProfile:
    path = _profile_path()
    if not path.exists():
        return VoiceProfile()
    arr = np.load(path)
    return VoiceProfile(embeddings=[row for row in arr])


def save_profile(profile: VoiceProfile) -> None:
    if not profile.embeddings:
        return
    path = _profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, np.stack(profile.embeddings))


def _centroid(profile: VoiceProfile) -> np.ndarray | None:
    if not profile.embeddings:
        return None
    c = np.mean(profile.embeddings, axis=0)
    return c / (np.linalg.norm(c) + 1e-9)


def match_score(embedding: np.ndarray, profile: VoiceProfile) -> float | None:
    """新样本跟已有声纹参照的质心比对相似度;参照还没建立(冷启动)返回 None。"""
    centroid = _centroid(profile)
    if centroid is None:
        return None
    return cosine_similarity(embedding, centroid)


def update_profile(profile: VoiceProfile, new_embedding: np.ndarray, threshold: float) -> VoiceProfile:
    """把新样本采纳进声纹参照。

    防污染:参照已经建立时,新样本要先过一遍跟现有质心的比对,对不上就不采纳
    (大概率这次根本不是本人在说话,采纳进去只会把参照带偏)。参照为空(真正
    的冷启动第一条样本)天然采纳,没有可比对的对象。
    """
    embeddings = profile.embeddings
    if embeddings:
        score = match_score(new_embedding, profile)
        if score is not None and score < threshold:
            return profile
    updated = embeddings + [new_embedding]
    if len(updated) > _PROFILE_MAX_SAMPLES:
        updated = updated[-_PROFILE_MAX_SAMPLES:]
    return VoiceProfile(embeddings=updated)
