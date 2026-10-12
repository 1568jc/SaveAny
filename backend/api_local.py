"""本地视频上传解析相关 API 路由（独立模块，通过 include_router 挂载）

本模块只负责「把本地视频变成字幕」这一段新增流程，
后续的 AI 总结 / 思维导图 / 视频问答 / 字幕导出全部复用现有能力：
  - 总结与思维导图：复用 summarizer.VideoSummarizer（与 /api/summarize 同一份实现）
  - 视频问答：复用现有 /api/chat（前端传 subtitle_text 即可，无需改后端）
  - 字幕导出：复用前端已有的 SRT/VTT/TXT 导出逻辑
"""

import asyncio
import json
import os
from collections.abc import AsyncIterable

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel

import admin
import local_video
from auth import get_current_user, get_optional_user
from database import FREE_DAILY_SUMMARY_LIMIT, check_and_increment_summary

router = APIRouter(prefix="/api/local", tags=["本地视频"])

CHUNK_SIZE = 1024 * 1024  # 1MB 一块写盘，避免整个文件进内存

# multipart 边界/头部的额外开销，留 2MB 余量
_MULTIPART_OVERHEAD = 2 * 1024 * 1024
MAX_UPLOAD_BODY = local_video.MAX_FILE_SIZE + _MULTIPART_OVERHEAD

# 只对上传接口生效，其余路由（包括原有的下载/解析）完全不受影响
_LIMITED_PATHS = frozenset({"/api/local/upload"})


class UploadSizeLimitMiddleware:
    """在请求体被解析之前限制上传大小的纯 ASGI 中间件。

    为什么必须放在中间件层：FastAPI 会在执行路由函数（以及鉴权依赖）之前
    先 `await request.form()` 把整个 multipart 解析完，Starlette 对「文件」
    part 不设上限，会一路写进系统临时目录。也就是说写在处理函数里的
    MAX_FILE_SIZE 校验永远是事后检查——字节早就落盘了，未登录用户也能
    靠超大请求把磁盘写满。只有在 receive 层拦截才真正有效。
    """

    def __init__(self, app, max_body_size: int = MAX_UPLOAD_BODY):
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") not in _LIMITED_PATHS:
            return await self.app(scope, receive, send)

        content_length = None
        auth_header = None
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                content_length = value
            elif name == b"authorization":
                auth_header = value

        # 1) 鉴权前置：依赖注入里的鉴权要等 body 解析完才跑，
        #    那时字节已经落盘了。这里先做一次廉价的 JWT 校验（不查库），
        #    让匿名请求连一个字节都传不进来。完整校验仍由端点依赖负责。
        payload = self._decode_token(auth_header)
        if payload is None:
            return await self._reject(send, 401, "请先登录后再上传视频")

        # 管理员放宽上限（仍保留有限值，避免误操作把磁盘写满）
        if admin.is_admin_email(payload.get("email")):
            max_body = admin.ADMIN_MAX_FILE_SIZE + _MULTIPART_OVERHEAD
            limit_mb = admin.ADMIN_MAX_FILE_SIZE // 1024 // 1024
        else:
            max_body = self.max_body_size
            limit_mb = local_video.MAX_FILE_SIZE // 1024 // 1024

        # 2) 有 Content-Length 时直接预检，连一个字节都不用读
        if content_length is not None:
            try:
                if int(content_length) > max_body:
                    return await self._reject(send, 413, f"文件过大，最大支持 {limit_mb}MB")
            except ValueError:
                pass

        # 3) 没有 Content-Length（chunked）时，边收边计数
        received = 0
        too_large = False

        async def limited_receive():
            nonlocal received, too_large
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > max_body:
                    too_large = True
                    # 断开输入流，让上游的 multipart 解析立即停止
                    return {"type": "http.disconnect"}
            return message

        response_started = False

        async def guarded_send(message):
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if too_large and not response_started:
                return await self._reject(send)
            raise

        if too_large and not response_started:
            await self._reject(send)

    @staticmethod
    def _decode_token(auth_header):
        """只验签名与有效期，不查库——目的是尽早挡掉匿名/伪造请求。
        返回 payload（含 email），失败返回 None。"""
        if not auth_header:
            return None
        try:
            raw = auth_header.decode("latin-1").strip()
        except Exception:
            return None
        if not raw.lower().startswith("bearer "):
            return None
        token = raw[7:].strip()
        if not token:
            return None
        try:
            from auth import decode_token
            return decode_token(token)
        except Exception:
            return None

    @staticmethod
    async def _reject(send, status: int = 413, message: str | None = None):
        if message is None:
            message = f"文件过大，最大支持 {local_video.MAX_FILE_SIZE // 1024 // 1024}MB"
        body = json.dumps(
            {"detail": {"success": False, "error": message}},
            ensure_ascii=False,
        ).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                (b"access-control-allow-origin", b"*"),
            ],
        })
        await send({"type": "http.response.body", "body": body})


class LocalSummarizeRequest(BaseModel):
    job_id: str
    language: str = "zh"


@router.get("/config")
async def get_local_config(user: dict | None = Depends(get_optional_user)):
    """前端用于展示上传限制说明。

    带上登录态时返回的是「该用户实际生效」的限额，
    这样管理员的前端校验会自动跟着放宽，不需要前端另写一套判断。
    """
    available, reason = local_video.asr_ready()
    is_admin = admin.is_admin(user)

    max_size = admin.ADMIN_MAX_FILE_SIZE if is_admin else local_video.MAX_FILE_SIZE
    max_duration = 0 if is_admin else local_video.MAX_DURATION_SECONDS

    return {
        "success": True,
        "data": {
            "max_file_size": max_size,
            "max_file_size_mb": max_size // 1024 // 1024,
            "max_duration": max_duration,
            "max_duration_minutes": max_duration // 60,
            "allowed_extensions": sorted(local_video.ALLOWED_EXTENSIONS),
            "asr_available": available,
            "asr_unavailable_reason": reason,
            "is_admin": is_admin,
        },
    }


@router.post("/upload")
async def upload_local_video(
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """上传本地视频（必须登录）。落盘后立即返回 job_id，处理在后台串行进行。"""
    filename = file.filename or ""
    is_admin = admin.is_admin(user)
    size_limit = admin.ADMIN_MAX_FILE_SIZE if is_admin else local_video.MAX_FILE_SIZE

    # 先做不需要落盘的快速校验
    declared_size = None
    if file.size is not None:
        declared_size = file.size
    try:
        local_video.validate_upload_meta(
            filename, file.content_type or "", declared_size, max_file_size=size_limit
        )
    except local_video.LocalVideoError as e:
        raise HTTPException(status_code=400, detail={"success": False, "error": str(e)})

    available, reason = local_video.asr_ready()
    if not available:
        raise HTTPException(status_code=503, detail={
            "success": False,
            "error": f"{reason}，请联系管理员",
        })

    # 处理是串行的，限制单用户在跑的任务数，避免一个账号占满队列（管理员不受限）
    if not is_admin and local_video.count_active_jobs(user["id"]) >= local_video.MAX_ACTIVE_JOBS_PER_USER:
        raise HTTPException(status_code=429, detail={
            "success": False,
            "error": "你有视频正在处理中，请等它完成后再上传新的视频",
        })

    job_id = local_video.create_job(user["id"], filename, declared_size or 0, is_admin=is_admin)
    work_dir = os.path.join(local_video.UPLOAD_DIR, job_id)
    os.makedirs(work_dir, exist_ok=True)
    # 保留原始扩展名（经白名单规范化），部分容器 ffmpeg 靠后缀判断更稳
    video_path = os.path.join(work_dir, "source" + local_video.safe_extension(filename))

    written = 0
    try:
        with open(video_path, "wb") as out:
            while True:
                chunk = await file.read(CHUNK_SIZE)
                if not chunk:
                    break
                written += len(chunk)
                if written > size_limit:
                    raise local_video.LocalVideoError(
                        f"文件过大，最大支持 {size_limit // 1024 // 1024}MB"
                    )
                out.write(chunk)
    except local_video.LocalVideoError as e:
        local_video.delete_job(job_id, user["id"])
        raise HTTPException(status_code=400, detail={"success": False, "error": str(e)})
    except Exception as e:
        local_video.delete_job(job_id, user["id"])
        raise HTTPException(status_code=500, detail={
            "success": False, "error": f"文件保存失败：{e}",
        })
    finally:
        await file.close()

    if written == 0:
        local_video.delete_job(job_id, user["id"])
        raise HTTPException(status_code=400, detail={
            "success": False, "error": "上传的文件为空，请重新选择",
        })

    local_video._update_job(job_id, video_path=video_path, filesize=written)
    local_video.cleanup_expired_jobs()
    local_video.submit_processing(job_id)

    return {
        "success": True,
        "data": {
            "job_id": job_id,
            "filename": filename,
            "filesize": written,
            "status": "pending",
        },
    }


@router.get("/status/{job_id}")
async def get_local_status(job_id: str, user: dict = Depends(get_current_user)):
    """轮询处理状态"""
    job = local_video.get_job(job_id, user["id"])
    if not job:
        raise HTTPException(status_code=404, detail={
            "success": False, "error": "任务不存在或已被清理，请重新上传",
        })
    return {"success": True, "data": local_video.job_status_payload(job)}


@router.delete("/job/{job_id}")
async def delete_local_job(job_id: str, user: dict = Depends(get_current_user)):
    """删除上传的视频和生成结果"""
    ok = local_video.delete_job(job_id, user["id"])
    if not ok:
        raise HTTPException(status_code=404, detail={
            "success": False, "error": "任务不存在或已被删除",
        })
    return {"success": True, "data": {"job_id": job_id, "deleted": True}}


@router.post("/summarize", response_class=EventSourceResponse)
async def summarize_local_video(
    req: LocalSummarizeRequest,
    user: dict = Depends(get_current_user),
) -> AsyncIterable[ServerSentEvent]:
    """
    本地视频 AI 总结（SSE 流式）。

    事件类型与 /api/summarize 完全一致：subtitle / summary / mindmap / quota / done / error，
    因此前端 VideoSummary 组件的渲染、字幕导出、思维导图导出逻辑可以原样复用。
    """
    job = local_video.get_job(req.job_id, user["id"])
    if not job:
        yield ServerSentEvent(
            raw_data=json.dumps({"message": "任务不存在或已被清理，请重新上传"}, ensure_ascii=False),
            event="error",
        )
        return

    if job["status"] == "failed":
        yield ServerSentEvent(
            raw_data=json.dumps({"message": job["error"] or "视频处理失败"}, ensure_ascii=False),
            event="error",
        )
        return

    if job["status"] != "done" or not job.get("subtitle"):
        yield ServerSentEvent(
            raw_data=json.dumps({"message": "视频仍在处理中，请稍候"}, ensure_ascii=False),
            event="error",
        )
        return

    # 配额与在线视频共用同一套规则（VIP 无限，免费用户每日 N 次）；管理员不计数
    if admin.is_admin(user):
        remaining = -1
    else:
        allowed, remaining = check_and_increment_summary(user["id"])
        if not allowed:
            yield ServerSentEvent(
                raw_data=json.dumps({
                    "message": f"今日免费 AI 总结次数已用完（每日 {FREE_DAILY_SUMMARY_LIMIT} 次），开通 VIP 可无限使用",
                    "need_login": False,
                    "need_vip": True,
                }, ensure_ascii=False),
                event="error",
            )
            return

    try:
        subtitle_data = job["subtitle"]

        yield ServerSentEvent(
            raw_data=json.dumps(subtitle_data, ensure_ascii=False),
            event="subtitle",
        )

        full_text = subtitle_data["full_text"]

        # 复用现有 summarizer，不新建任何一套总结/问答系统
        from api_summarize import _get_summarizer
        summarizer = _get_summarizer()

        for token in summarizer.summarize_stream(full_text, req.language, segments=subtitle_data.get("segments")):
            yield ServerSentEvent(raw_data=json.dumps(token, ensure_ascii=False), event="summary")

        loop = asyncio.get_event_loop()
        mindmap_md = await loop.run_in_executor(
            None, summarizer.generate_mindmap, full_text, req.language
        )
        yield ServerSentEvent(
            raw_data=json.dumps({"markdown": mindmap_md}, ensure_ascii=False),
            event="mindmap",
        )

        yield ServerSentEvent(
            raw_data=json.dumps(
                {"remaining": remaining, "limit": FREE_DAILY_SUMMARY_LIMIT},
                ensure_ascii=False,
            ),
            event="quota",
        )

        yield ServerSentEvent(raw_data="[DONE]", event="done")

    except Exception as e:
        yield ServerSentEvent(
            raw_data=json.dumps({"message": f"总结失败: {str(e)}"}, ensure_ascii=False),
            event="error",
        )
