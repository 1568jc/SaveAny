"""AI 视频总结相关 API 路由（独立模块，通过 include_router 挂载）"""

import asyncio
import json
from collections.abc import AsyncIterable

from fastapi import APIRouter, Depends, HTTPException
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel

from auth import get_optional_user
from database import check_and_increment_summary, FREE_DAILY_SUMMARY_LIMIT
from douyin import is_douyin_url
from online_asr import transcribe_online_video

router = APIRouter(prefix="/api", tags=["AI 总结"])


class SummarizeRequest(BaseModel):
    url: str
    language: str = "zh"
    # 前端解析时已拿到的 CDN 直链（抖音用），避免总结时重复请求平台 API 触发风控
    direct_url: str = ""


class ChatRequest(BaseModel):
    url: str
    question: str
    subtitle_text: str = ""
    # 可选：字幕分段（含时间戳），传入后回答会引用时间点
    segments: list | None = None


class TranslateRequest(BaseModel):
    # 字幕分段（含时间戳），由前端从已有的 subtitle 数据直接传入
    segments: list
    target_lang: str = "zh"  # zh=翻译成中文，en=翻译成英文


def _check_summary_permission(user: dict | None):
    """
    检查 AI 总结权限。
    未登录用户：不允许使用。
    免费用户：每日限制次数。
    VIP 用户：无限制。
    返回 (allowed, remaining, message)
    """
    if not user:
        return False, 0, "请先登录后使用 AI 总结功能"

    # 管理员（ADMIN_EMAILS 指定）不计次数
    import admin
    if admin.is_admin(user):
        return True, -1, None

    allowed, remaining = check_and_increment_summary(user["id"])
    if not allowed:
        return False, 0, f"今日免费 AI 总结次数已用完（每日 {FREE_DAILY_SUMMARY_LIMIT} 次），开通 VIP 可无限使用"

    return True, remaining, None


def _get_summarizer():
    """延迟初始化 VideoSummarizer（仅在首次调用时创建）"""
    from summarizer import VideoSummarizer
    if not hasattr(_get_summarizer, "_instance"):
        try:
            _get_summarizer._instance = VideoSummarizer()
        except ValueError as e:
            raise HTTPException(status_code=500, detail=str(e))
    return _get_summarizer._instance


def _get_extractor():
    """延迟初始化 SubtitleExtractor"""
    from summarizer import SubtitleExtractor
    if not hasattr(_get_extractor, "_instance"):
        _get_extractor._instance = SubtitleExtractor()
    return _get_extractor._instance


async def _asr_events(url: str, direct_url: str, out: dict) -> AsyncIterable[ServerSentEvent]:
    """在线视频语音识别：把转写进度实时推给前端（subtitle_progress 事件）。

    ASR 在工作线程里跑（可能持续几分钟），进度回调经 call_soon_threadsafe
    桥接进事件循环的消费队列，主协程边收边发 SSE。
    结果写入 out["data"]，异常写入 out["error"]（async 生成器无法用返回值传数据）。
    """
    loop = asyncio.get_event_loop()
    progress_q = asyncio.Queue()

    last_pct = [-1]

    def on_progress(pct):
        # 按整百分比去重，最多 100 个事件，不会刷屏
        if pct != last_pct[0]:
            last_pct[0] = pct
            loop.call_soon_threadsafe(progress_q.put_nowait, pct)

    yield ServerSentEvent(
        raw_data=json.dumps({"stage": "download"}, ensure_ascii=False),
        event="subtitle_progress",
    )

    future = loop.run_in_executor(
        None, lambda: transcribe_online_video(url, direct_url, on_progress=on_progress)
    )
    while not (future.done() and progress_q.empty()):
        try:
            pct = await asyncio.wait_for(progress_q.get(), timeout=0.3)
        except asyncio.TimeoutError:
            continue
        yield ServerSentEvent(
            raw_data=json.dumps({"stage": "transcribe", "percent": pct}, ensure_ascii=False),
            event="subtitle_progress",
        )

    # future 完成时可能还有已排队未执行的回调，让出一次事件循环再排空队列
    await asyncio.sleep(0)
    while not progress_q.empty():
        pct = progress_q.get_nowait()
        yield ServerSentEvent(
            raw_data=json.dumps({"stage": "transcribe", "percent": pct}, ensure_ascii=False),
            event="subtitle_progress",
        )

    try:
        out["data"] = future.result()
    except Exception as e:
        out["error"] = e


@router.post("/summarize", response_class=EventSourceResponse)
async def summarize_video(req: SummarizeRequest, user: dict | None = Depends(get_optional_user)) -> AsyncIterable[ServerSentEvent]:
    """
    AI 视频总结（SSE 流式）
    事件类型: subtitle / summary / mindmap / done / error / quota
    """
    allowed, remaining, message = _check_summary_permission(user)
    if not allowed:
        yield ServerSentEvent(
            raw_data=json.dumps({"message": message, "need_login": user is None, "need_vip": user is not None}, ensure_ascii=False),
            event="error",
        )
        return

    try:
        loop = asyncio.get_event_loop()
        extractor = _get_extractor()
        if is_douyin_url(req.url):
            # 抖音没有平台字幕（yt-dlp 也不支持其用户页），直接走语音识别回退；
            # 优先用前端解析时拿到的直链，避免再次请求抖音 API
            asr_out: dict = {}
            async for event in _asr_events(req.url, req.direct_url, asr_out):
                yield event
            if "error" in asr_out:
                raise asr_out["error"]
            subtitle_data = asr_out["data"]
        else:
            subtitle_data = await loop.run_in_executor(
                None, extractor.extract, req.url
            )
            if not subtitle_data["has_subtitle"]:
                # 平台没有字幕轨道时，下载音频用 whisper 转写兜底
                asr_out = {}
                async for event in _asr_events(req.url, "", asr_out):
                    yield event
                if "error" in asr_out:
                    raise asr_out["error"]
                subtitle_data = asr_out["data"]

        yield ServerSentEvent(
            raw_data=json.dumps(subtitle_data, ensure_ascii=False),
            event="subtitle",
        )

        if not subtitle_data["has_subtitle"]:
            yield ServerSentEvent(
                raw_data=json.dumps({"message": "该视频没有可用的字幕，无法生成总结"}, ensure_ascii=False),
                event="error",
            )
            return

        full_text = subtitle_data["full_text"]
        summarizer = _get_summarizer()

        for token in summarizer.summarize_stream(full_text, req.language, segments=subtitle_data.get("segments")):
            yield ServerSentEvent(raw_data=json.dumps(token, ensure_ascii=False), event="summary")

        mindmap_md = await loop.run_in_executor(
            None, summarizer.generate_mindmap, full_text, req.language
        )
        yield ServerSentEvent(
            raw_data=json.dumps({"markdown": mindmap_md}, ensure_ascii=False),
            event="mindmap",
        )

        quota_info = {"remaining": remaining, "limit": FREE_DAILY_SUMMARY_LIMIT}
        yield ServerSentEvent(
            raw_data=json.dumps(quota_info, ensure_ascii=False),
            event="quota",
        )

        yield ServerSentEvent(raw_data="[DONE]", event="done")

    except Exception as e:
        yield ServerSentEvent(
            raw_data=json.dumps({"message": f"总结失败: {str(e)}"}, ensure_ascii=False),
            event="error",
        )


@router.post("/chat", response_class=EventSourceResponse)
async def chat_with_video(req: ChatRequest, user: dict | None = Depends(get_optional_user)) -> AsyncIterable[ServerSentEvent]:
    """AI 视频问答（SSE 流式）"""
    try:
        if not req.subtitle_text.strip():
            loop = asyncio.get_event_loop()
            extractor = _get_extractor()
            subtitle_data = await loop.run_in_executor(
                None, extractor.extract, req.url
            )
            if not subtitle_data["has_subtitle"]:
                yield ServerSentEvent(
                    raw_data=json.dumps({"message": "该视频没有可用的字幕，无法回答问题"}, ensure_ascii=False),
                    event="error",
                )
                return
            subtitle_text = subtitle_data["full_text"]
            segments = subtitle_data.get("segments")
        else:
            subtitle_text = req.subtitle_text
            segments = req.segments

        summarizer = _get_summarizer()
        for token in summarizer.chat_stream(subtitle_text, req.question, segments=segments):
            yield ServerSentEvent(raw_data=json.dumps(token, ensure_ascii=False), event="answer")

        yield ServerSentEvent(raw_data="[DONE]", event="done")

    except Exception as e:
        yield ServerSentEvent(
            raw_data=json.dumps({"message": f"回答失败: {str(e)}"}, ensure_ascii=False),
            event="error",
        )


@router.post("/translate", response_class=EventSourceResponse)
async def translate_subtitle(req: TranslateRequest, user: dict | None = Depends(get_optional_user)) -> AsyncIterable[ServerSentEvent]:
    """
    字幕翻译（SSE 流式）
    事件类型: translate / quota / done / error
    权限与 AI 总结共用一套规则：需登录，免费用户每次计 1 次每日配额，VIP/管理员不限。
    """
    allowed, remaining, message = _check_summary_permission(user)
    if not allowed:
        yield ServerSentEvent(
            raw_data=json.dumps({"message": message, "need_login": user is None, "need_vip": user is not None}, ensure_ascii=False),
            event="error",
        )
        return

    if req.target_lang not in ("zh", "en"):
        yield ServerSentEvent(
            raw_data=json.dumps({"message": "暂不支持的目标语言"}, ensure_ascii=False),
            event="error",
        )
        return

    if not req.segments:
        yield ServerSentEvent(
            raw_data=json.dumps({"message": "没有可翻译的字幕内容"}, ensure_ascii=False),
            event="error",
        )
        return

    try:
        summarizer = _get_summarizer()
        for token in summarizer.translate_stream(req.segments, req.target_lang):
            yield ServerSentEvent(raw_data=json.dumps(token, ensure_ascii=False), event="translate")

        quota_info = {"remaining": remaining, "limit": FREE_DAILY_SUMMARY_LIMIT}
        yield ServerSentEvent(
            raw_data=json.dumps(quota_info, ensure_ascii=False),
            event="quota",
        )

        yield ServerSentEvent(raw_data="[DONE]", event="done")

    except Exception as e:
        yield ServerSentEvent(
            raw_data=json.dumps({"message": f"翻译失败: {str(e)}"}, ensure_ascii=False),
            event="error",
        )
