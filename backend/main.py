import os
import re
import asyncio
from contextlib import asynccontextmanager
from urllib.parse import unquote

from dotenv import load_dotenv
load_dotenv()

# 必须在导入 downloader（内部 import yt_dlp）之前执行，
# 这样自动升级后的新版 yt-dlp 才会被本进程加载，无需重启。
from ytdlp_manager import auto_upgrade_on_startup
auto_upgrade_on_startup()

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from downloader import VideoDownloader
from douyin import DouyinParser, is_douyin_url
from database import init_db
from friendly_error import friendly_error
from ytdlp_manager import get_installed_version, get_latest_version, is_outdated


downloader = VideoDownloader()
douyin_parser = DouyinParser(download_dir=downloader.DOWNLOAD_DIR)


async def _periodic_local_cleanup():
    """定期回收过期的本地视频任务。

    否则清理只在有新上传时才触发，服务空闲期间过期任务会一直占着内存和磁盘。
    """
    import local_video
    while True:
        try:
            await asyncio.sleep(300)
            await asyncio.get_event_loop().run_in_executor(
                None, local_video.cleanup_expired_jobs
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    cleanup_task = asyncio.create_task(_periodic_local_cleanup())
    yield
    cleanup_task.cancel()
    download_dir = downloader.DOWNLOAD_DIR
    if os.path.exists(download_dir):
        for f in os.listdir(download_dir):
            try:
                os.remove(os.path.join(download_dir, f))
            except OSError:
                pass
    # 停止本地视频后台处理并清空上传内容（不长期保存用户视频）
    try:
        import local_video
        local_video.shutdown()
    except Exception:
        pass


app = FastAPI(
    title="万能视频下载器 API",
    description="基于 yt-dlp 的万能视频下载服务，支持 1800+ 平台",
    version="1.0.0",
    lifespan=lifespan,
)

# 先注册 = 处于内层，保证 CORS 仍在最外层正常工作。
# 仅拦截 /api/local/upload，不影响任何原有接口。
from api_local import UploadSizeLimitMiddleware

app.add_middleware(UploadSizeLimitMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ParseRequest(BaseModel):
    url: str


_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def _extract_first_url(text: str) -> str:
    """从分享口令等文本中提取第一个 URL；找不到则原样返回"""
    m = _URL_RE.search(text)
    return m.group(0).rstrip(").,;!?") if m else text


class DownloadRequest(BaseModel):
    url: str
    format_id: str = "bestvideo+bestaudio/best"


@app.get("/api/health")
async def health_check():
    """健康检查：报告 yt-dlp 版本状态和 ffmpeg 可用性，便于排查解析故障"""
    return {
        "status": "ok",
        "message": "万能视频下载器服务运行中",
        "yt_dlp": {
            "current": get_installed_version(),
            "latest": get_latest_version(),
            "outdated": is_outdated(),
        },
        "ffmpeg": downloader.has_ffmpeg,
    }


@app.post("/api/parse")
async def parse_video(req: ParseRequest):
    """解析视频信息（抖音走专用模块，其他走 yt-dlp）"""
    try:
        loop = asyncio.get_event_loop()
        if is_douyin_url(req.url):
            result = await loop.run_in_executor(None, douyin_parser.parse, req.url)
        else:
            result = await loop.run_in_executor(None, downloader.parse_video, _extract_first_url(req.url))
        return {"success": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail={
            "success": False,
            "error": friendly_error(e)
        })


@app.post("/api/download")
async def download_video(req: DownloadRequest):
    """服务端下载视频后提供文件下载（抖音走专用模块）"""
    try:
        loop = asyncio.get_event_loop()
        if is_douyin_url(req.url):
            result = await loop.run_in_executor(None, douyin_parser.download, req.url)
        else:
            result = await loop.run_in_executor(
                None, downloader.download_video, _extract_first_url(req.url), req.format_id
            )
        filepath = result["filepath"]
        if not os.path.exists(filepath):
            raise HTTPException(status_code=500, detail="下载的文件不存在")

        return FileResponse(
            path=filepath,
            filename=result["filename"],
            media_type="application/octet-stream",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail={
            "success": False,
            "error": friendly_error(e)
        })


@app.post("/api/direct-url")
async def get_direct_url(req: DownloadRequest):
    """获取视频直链"""
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, downloader.get_direct_url, _extract_first_url(req.url), req.format_id
        )
        return {"success": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail={
            "success": False,
            "error": friendly_error(e)
        })


@app.get("/api/proxy/thumbnail")
async def proxy_thumbnail(url: str = Query(..., description="缩略图URL")):
    """代理获取视频缩略图，绕过防盗链"""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": url,
            })
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "image/jpeg")
            return StreamingResponse(
                iter([resp.content]),
                media_type=content_type,
                headers={"Cache-Control": "public, max-age=86400"},
            )
    except Exception:
        raise HTTPException(status_code=502, detail="缩略图加载失败")


# 挂载功能模块路由
from api_summarize import router as summarize_router
from api_auth import router as auth_router
from api_payment import router as payment_router
from api_local import router as local_router

app.include_router(summarize_router)
app.include_router(auth_router)
app.include_router(payment_router)
app.include_router(local_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
