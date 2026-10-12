"""在线视频的语音识别回退：平台无字幕时，下载音频并用 faster-whisper 转写。

抖音等平台没有字幕轨道，yt-dlp 提取字幕必然失败；该模块让这类视频也能使用
AI 总结 / 思维导图 / 问答。产出的字幕结构与 SubtitleExtractor.extract() 完全一致，
前端无需任何改动。

抖音场景优先使用前端解析时已经拿到的 CDN 直链，避免再次请求抖音 API 触发风控；
没有直链时才回退到 DouyinParser 重新解析。注意不能用 item_info 里的
music.play_url——那是背景音乐的独立音源，不是视频人声。
"""

import os
import tempfile

import yt_dlp

import local_video
from douyin import DouyinParser, is_douyin_url

# 与本地视频上传保持同一时长上限（60 分钟），防止超长视频把 CPU 占满
MAX_ASR_DURATION = local_video.MAX_DURATION_SECONDS


class OnlineASRError(Exception):
    """在线视频语音识别失败"""


def transcribe_online_video(url: str, direct_url: str = "", on_progress=None) -> dict:
    """对在线视频做语音识别，返回与 SubtitleExtractor.extract() 一致的结构。

    流程：下载音频/视频（校验时长）→ whisper 转写 → 清理临时文件。
    on_progress：可选的转写进度回调（0-100 整数），透传给 local_video.transcribe；
    不传时行为与之前完全一致。失败抛出 OnlineASRError。
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            if is_douyin_url(url):
                audio_path, duration = _douyin_audio(url, direct_url, tmp_dir)
            else:
                audio_path, duration = _download_audio_ytdlp(url, tmp_dir)
        except OnlineASRError:
            raise
        except Exception as e:
            raise OnlineASRError(f"音频获取失败: {e}")

        try:
            return local_video.transcribe(audio_path, duration, on_progress=on_progress)
        except Exception as e:
            raise OnlineASRError(str(e))


def _check_duration(duration):
    if duration and duration > MAX_ASR_DURATION:
        minutes = int(duration // 60)
        raise OnlineASRError(
            f"该视频没有平台字幕，且时长 {minutes} 分钟超过语音识别上限（60 分钟），暂无法生成 AI 总结"
        )


def _douyin_audio(url: str, direct_url: str, tmp_dir: str) -> tuple[str, float]:
    """抖音：下载视频本体再抽音轨（不用 music.play_url——那是背景音乐，没有人声）。

    两个下载途径，依次尝试：
    1. 前端解析时拿到的 CDN 直链（不请求抖音 API，最不容易触发风控）
    2. 直链失效（403/过期）时，重新解析拿一条新鲜直链再下载

    下载直接复用 DouyinParser 的会话与请求头——与「立即下载」走的是完全相同的
    请求特征，避免 httpx 默认头被 CDN WAF 拦截（403）。
    """
    from pathlib import Path

    parser = DouyinParser(download_dir=tmp_dir)
    video_path = Path(tmp_dir) / "video.mp4"
    errors = []

    if direct_url:
        try:
            parser._download_file(direct_url, video_path)
        except Exception as e:
            errors.append(f"直链下载失败: {e}")

    if not video_path.exists():
        try:
            item_info, _ = parser.parse_item(url)
            fresh_url = parser._get_media_url(item_info, mode="video")
            parser._download_file(fresh_url, video_path)
        except Exception as e:
            errors.append(f"重新解析下载失败: {e}")

    if not video_path.exists():
        raise OnlineASRError("；".join(errors) or "视频文件下载失败")

    # ffprobe 校验：确认是有效视频、有音轨，并拿到真实时长
    meta = local_video.probe_video(str(video_path), max_duration=MAX_ASR_DURATION)

    audio_path = os.path.join(tmp_dir, "audio.wav")
    local_video.extract_audio(str(video_path), audio_path)
    return audio_path, meta["duration"]


def _download_audio_ytdlp(url: str, tmp_dir: str) -> tuple[str, float]:
    """其他平台：yt-dlp 下载最佳音频流"""
    base_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL({**base_opts, "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise OnlineASRError("无法解析该视频链接")

    duration = info.get("duration") or 0
    _check_duration(duration)

    with yt_dlp.YoutubeDL({
        **base_opts,
        "format": "bestaudio/best",
        "outtmpl": os.path.join(tmp_dir, "audio.%(ext)s"),
    }) as ydl:
        ydl.download([url])

    for f in os.listdir(tmp_dir):
        if f.startswith("audio."):
            return os.path.join(tmp_dir, f), duration

    raise OnlineASRError("音频下载失败：未找到下载后的文件")
