"""本地视频处理模块：上传校验 -> ffmpeg 提取音频 -> faster-whisper 生成带时间戳字幕

设计原则：
1. 输出结构与 summarizer.SubtitleExtractor.extract() 完全一致，
   即 {has_subtitle, language, subtitle_type, segments[{start,end,text}], full_text}，
   这样后续的 AI 总结 / 思维导图 / 视频问答 / 字幕导出可以原样复用，无需任何改动。
2. 不写数据库：任务状态放内存，视频文件放临时目录，转写完成后立刻删除媒体文件。
3. 不修改任何在线视频解析逻辑。
"""

import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

# Anaconda 的 MKL 与 ctranslate2 各自带了一份 OpenMP 运行时，
# 同一进程加载两份会直接抛 "OMP: Error #15" 并终止进程。
# 必须在 faster-whisper / ctranslate2 被导入之前设置。
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

# ===== 业务限制（第一版本范围） =====
MAX_FILE_SIZE = 500 * 1024 * 1024        # 500MB
MAX_DURATION_SECONDS = 60 * 60           # 60 分钟
# 支持的容器格式。底层是 ffmpeg 解封装 + 提取音频，这些容器它都能处理；
# 真正的把关在 probe_video()：ffprobe 会确认文件确实含视频流和音轨，
# 所以改后缀名伪装的文件依然会被挡下。
ALLOWED_EXTENSIONS = {
    ".mp4", ".m4v", ".mov",          # MPEG-4 / QuickTime
    ".mkv", ".webm",                 # Matroska / WebM
    ".avi", ".wmv", ".asf",          # Windows 系
    ".flv", ".f4v",                  # Flash
    ".ts", ".mts", ".m2ts", ".mpg", ".mpeg", ".m2v",  # MPEG 传输/节目流
    ".3gp", ".3g2",                  # 手机录制
    ".ogv", ".rmvb", ".rm", ".vob", ".divx",
}

# 浏览器对 mkv/flv/ts 等格式经常给不出 MIME（空）或给 application/octet-stream，
# 因此 MIME 只用来挡明显不对的类型（文本、图片、压缩包），不作为主要依据。
_REJECTED_CONTENT_PREFIXES = ("text/", "image/", "audio/")
_REJECTED_CONTENT_TYPES = {
    "application/zip", "application/x-rar-compressed", "application/x-7z-compressed",
    "application/pdf", "application/json", "application/javascript",
    "application/x-msdownload", "application/x-executable",
}


def format_allowed_extensions() -> str:
    """给用户看的格式清单，如 'MP4 / MOV / MKV ...'"""
    preferred = [".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".wmv", ".ts"]
    shown = [e for e in preferred if e in ALLOWED_EXTENSIONS]
    return " / ".join(e.lstrip(".").upper() for e in shown) + " 等"


def safe_extension(filename: str) -> str:
    """取出规范化后的扩展名，仅允许白名单内的值用于拼接落盘路径"""
    ext = os.path.splitext(filename or "")[1].lower()
    return ext if ext in ALLOWED_EXTENSIONS else ".mp4"

# 任务结果保留时长（不长期保存用户视频/结果）
JOB_TTL_SECONDS = 2 * 60 * 60            # 2 小时

# 尚未产出最终结果、仍占用后台线程的状态
ACTIVE_STATUSES = ("pending", "probing", "extracting", "transcribing")

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")

# Whisper 配置（可通过环境变量覆盖）
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "base")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "") or None  # 空表示自动检测

# 处理串行化：语音识别是 CPU 密集型，同时跑多个会互相拖慢
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-video")

# 单个用户同时进行中的任务数上限：处理是串行的，
# 不限制的话一个账号连续提交就能把队列占满，别人一直排不上。
MAX_ACTIVE_JOBS_PER_USER = 1

# 任务状态表（内存），key = job_id
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

# 进程退出信号：让后台线程在阶段之间尽快收手
_shutting_down = threading.Event()

# 当前正在运行的 ffmpeg/ffprobe 子进程，退出时需要主动终止
_active_proc = None
_active_proc_lock = threading.Lock()


logger = logging.getLogger("local_video")


class LocalVideoError(Exception):
    """带用户可读中文提示的业务异常"""


def _log_tool_error(tool: str, stderr: bytes):
    """外部工具的原始报错只写服务端日志，不回传给前端"""
    try:
        detail = (stderr or b"").decode("utf-8", errors="ignore").strip()
    except Exception:
        detail = "<解码失败>"
    if detail:
        logger.error("%s 执行失败: %s", tool, detail[:1000])


# ===== ffmpeg / ffprobe 定位 =====

def _find_binary(name: str) -> Optional[str]:
    """查找 ffmpeg / ffprobe，优先 PATH，其次项目自带的 tools/ffmpeg/bin"""
    found = shutil.which(name)
    if found:
        return found

    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    candidate = os.path.join(project_root, "tools", "ffmpeg", "bin", f"{name}.exe")
    if os.path.exists(candidate):
        return candidate

    try:
        import static_ffmpeg
        paths = static_ffmpeg.run.get_or_fetch_platform_executables_else_raise()
        for p in paths:
            if os.path.basename(p).startswith(name):
                return p
    except Exception:
        pass
    return None


FFMPEG_BIN = _find_binary("ffmpeg")
FFPROBE_BIN = _find_binary("ffprobe")


def _no_window_kwargs() -> dict:
    """Windows 下不弹出黑色控制台窗口"""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def _run_tracked(cmd: list, timeout: int):
    """运行 ffmpeg/ffprobe，并登记进程句柄，便于服务退出时终止。

    用 Popen 而非 run，是为了让 lifespan 停机时能主动 terminate，
    否则一个正在转码的长视频会把进程退出拖住几分钟。
    """
    global _active_proc
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **_no_window_kwargs(),
    )
    with _active_proc_lock:
        _active_proc = proc
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    finally:
        with _active_proc_lock:
            if _active_proc is proc:
                _active_proc = None
    return proc.returncode, stdout, stderr


def terminate_active_process():
    """停机时终止正在运行的 ffmpeg/ffprobe"""
    with _active_proc_lock:
        proc = _active_proc
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass


def shutdown():
    """服务退出：停止排队任务、终止子进程、清空上传内容"""
    _shutting_down.set()
    terminate_active_process()
    try:
        _executor.shutdown(wait=False, cancel_futures=True)
    except TypeError:                     # Python < 3.9 没有 cancel_futures
        _executor.shutdown(wait=False)
    cleanup_all()


# ===== 任务状态管理 =====

def create_job(user_id: int, filename: str, filesize: int, is_admin: bool = False) -> str:
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "user_id": user_id,
            "is_admin": is_admin,
            "filename": filename,
            "filesize": filesize,
            "status": "pending",          # pending/probing/extracting/transcribing/done/failed
            "progress": 0,
            "stage_text": "排队中...",
            "error": None,
            "duration": None,
            "duration_string": "",
            "subtitle": None,             # 与 SubtitleExtractor.extract() 同构
            "video_path": None,
            "audio_path": None,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
    return job_id


def _update_job(job_id: str, **fields):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job.update(fields)
        job["updated_at"] = time.time()


def count_active_jobs(user_id: int) -> int:
    """该用户当前处于排队/处理中的任务数"""
    with _jobs_lock:
        return sum(
            1 for j in _jobs.values()
            if j["user_id"] == user_id and j["status"] in ACTIVE_STATUSES
        )


def get_job(job_id: str, user_id: int) -> Optional[dict]:
    """获取任务，同时校验归属（防止越权访问他人上传结果）"""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or job["user_id"] != user_id:
            return None
        return dict(job)


def job_status_payload(job: dict) -> dict:
    """给前端的状态数据（不含服务器文件路径）"""
    subtitle = job.get("subtitle")
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "progress": job["progress"],
        "stage_text": job["stage_text"],
        "error": job["error"],
        "filename": job["filename"],
        "filesize": job["filesize"],
        "duration": job["duration"],
        "duration_string": job["duration_string"],
        "segment_count": len(subtitle["segments"]) if subtitle else 0,
    }


def delete_job(job_id: str, user_id: int) -> bool:
    """删除上传的视频和生成结果"""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or job["user_id"] != user_id:
            return False
        _jobs.pop(job_id, None)
    _cleanup_files(job)
    return True


def _cleanup_files(job: dict):
    for key in ("video_path", "audio_path"):
        path = job.get(key)
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
    work_dir = os.path.join(UPLOAD_DIR, job["job_id"])
    if os.path.isdir(work_dir):
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except OSError:
            pass


def cleanup_expired_jobs():
    """清理过期任务（不长期保存用户视频与结果）

    绝不回收仍在处理中的任务：长视频转写可能超过 TTL，
    误删会把后台线程正在读的源文件删掉，导致处理中途失败。
    """
    now = time.time()
    expired = []
    with _jobs_lock:
        for job_id, job in list(_jobs.items()):
            if job["status"] in ACTIVE_STATUSES:
                continue
            if now - job["created_at"] > JOB_TTL_SECONDS:
                expired.append(_jobs.pop(job_id))
    for job in expired:
        _cleanup_files(job)


def cleanup_all():
    """进程退出时清空所有上传内容"""
    with _jobs_lock:
        jobs = list(_jobs.values())
        _jobs.clear()
    for job in jobs:
        _cleanup_files(job)
    if os.path.isdir(UPLOAD_DIR):
        shutil.rmtree(UPLOAD_DIR, ignore_errors=True)


# ===== 校验 =====

def validate_upload_meta(
    filename: str,
    content_type: str,
    filesize: Optional[int],
    max_file_size: Optional[int] = None,
):
    """在真正落盘前做快速校验，尽早给出明确错误"""
    limit = max_file_size or MAX_FILE_SIZE
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        shown = ext.lstrip(".").upper() if ext else "未知"
        raise LocalVideoError(
            f"暂不支持 {shown} 格式，目前支持 {format_allowed_extensions()}，请转换后再上传"
        )

    ct = (content_type or "").lower().strip()
    if ct and (ct in _REJECTED_CONTENT_TYPES or ct.startswith(_REJECTED_CONTENT_PREFIXES)):
        raise LocalVideoError(f"文件类型不正确（{ct}），请上传视频文件")

    if filesize is not None and filesize > limit:
        raise LocalVideoError(
            f"文件过大（{filesize / 1024 / 1024:.0f}MB），最大支持 {limit // 1024 // 1024}MB"
        )


def probe_video(path: str, max_duration: Optional[int] = None) -> dict:
    """用 ffprobe 探测真实媒体信息，同时确认文件确实是视频

    max_duration 传 0 表示不限制时长（管理员）。
    """
    if not FFPROBE_BIN:
        raise LocalVideoError("服务器未安装 ffprobe，无法处理本地视频，请联系管理员")

    cmd = [
        FFPROBE_BIN, "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        path,
    ]
    try:
        returncode, stdout, stderr = _run_tracked(cmd, timeout=120)
    except subprocess.TimeoutExpired:
        raise LocalVideoError("视频文件解析超时，请确认文件未损坏")

    if returncode != 0:
        _log_tool_error("ffprobe", stderr)
        raise LocalVideoError("无法解析该视频文件，可能已损坏或不是有效的视频格式")

    try:
        info = json.loads(stdout.decode("utf-8", errors="ignore"))
    except json.JSONDecodeError:
        raise LocalVideoError("无法解析该视频文件的媒体信息")

    streams = info.get("streams", [])
    has_video = any(s.get("codec_type") == "video" for s in streams)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    if not has_video:
        raise LocalVideoError("该文件不包含视频流，请上传有效的视频文件")
    if not has_audio:
        raise LocalVideoError("该视频没有音轨，无法进行语音识别")

    duration = 0.0
    try:
        duration = float(info.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        for s in streams:
            try:
                duration = max(duration, float(s.get("duration") or 0))
            except (TypeError, ValueError):
                pass

    if duration <= 0:
        raise LocalVideoError("无法获取视频时长，请确认文件未损坏")

    limit = MAX_DURATION_SECONDS if max_duration is None else max_duration
    if limit and duration > limit:
        raise LocalVideoError(
            f"视频时长 {_format_duration(duration)} 超出限制，最长支持 {limit // 60} 分钟"
        )

    return {"duration": duration, "has_audio": has_audio}


def _format_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return "00:00"
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# ===== 音频提取 =====

def extract_audio(video_path: str, audio_path: str):
    """提取为 16kHz 单声道 WAV —— Whisper 的原生输入格式"""
    if not FFMPEG_BIN:
        raise LocalVideoError("服务器未安装 ffmpeg，无法处理本地视频，请联系管理员")

    cmd = [
        FFMPEG_BIN, "-y",
        "-i", video_path,
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-acodec", "pcm_s16le",
        "-loglevel", "error",
        audio_path,
    ]
    try:
        returncode, _, stderr = _run_tracked(cmd, timeout=30 * 60)
    except subprocess.TimeoutExpired:
        raise LocalVideoError("音频提取超时，请尝试更短的视频")

    if returncode != 0 or not os.path.exists(audio_path):
        # ffmpeg 的 stderr 含服务器绝对路径等内部信息，只写日志不回传前端
        _log_tool_error("ffmpeg", stderr)
        raise LocalVideoError("音频提取失败，请确认视频文件未损坏后重试")


# ===== 语音识别 =====

_opencc_converter = None


def _to_simplified(text: str, language: str) -> str:
    """Whisper 识别中文普通话常输出繁体，识别结果为中文时统一转简体。

    OpenCC 未安装时静默返回原文，不影响主流程。
    """
    global _opencc_converter
    if language != "zh":
        return text
    try:
        if _opencc_converter is None:
            from opencc import OpenCC
            _opencc_converter = OpenCC("t2s")
        return _opencc_converter.convert(text)
    except ImportError:
        return text


_model_lock = threading.Lock()
_model = None


def _get_model():
    """延迟加载 Whisper 模型（首次调用会下载模型文件，之后缓存复用）"""
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise LocalVideoError(
                "服务器未安装语音识别组件（faster-whisper），无法处理本地视频，请联系管理员"
            )
        try:
            _model = WhisperModel(
                WHISPER_MODEL_SIZE,
                device=WHISPER_DEVICE,
                compute_type=WHISPER_COMPUTE_TYPE,
            )
        except Exception as e:
            raise LocalVideoError(f"语音识别模型加载失败：{e}")
        return _model


def transcribe(audio_path: str, total_duration: float, on_progress=None) -> dict:
    """
    语音识别，返回结构与 SubtitleExtractor.extract() 完全一致，
    以便直接进入现有的 AI 总结 / 思维导图 / 问答 / 字幕导出流程。
    """
    model = _get_model()

    try:
        segments_iter, info = model.transcribe(
            audio_path,
            language=WHISPER_LANGUAGE,
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            # 引导模型输出简体中文（Whisper 识别普通话时经常默认输出繁体）
            initial_prompt="以下是简体中文普通话的语音转写。" if WHISPER_LANGUAGE in (None, "", "zh") else None,
        )
    except Exception as e:
        raise LocalVideoError(f"语音识别失败：{e}")

    segments = []
    try:
        for seg in segments_iter:
            text = (seg.text or "").strip()
            if text:
                text = _to_simplified(text, getattr(info, "language", "") or "")
            if text:
                segments.append({
                    "start": round(seg.start, 2),
                    "end": round(seg.end, 2),
                    "text": text,
                })
            if on_progress and total_duration > 0:
                on_progress(min(99, int(seg.end / total_duration * 100)))
    except Exception as e:
        raise LocalVideoError(f"语音识别过程中出错：{e}")

    if not segments:
        return {
            "has_subtitle": False,
            "language": getattr(info, "language", "") or "",
            "subtitle_type": "none",
            "segments": [],
            "full_text": "",
        }

    full_text = " ".join(seg["text"] for seg in segments)
    return {
        "has_subtitle": True,
        "language": getattr(info, "language", "") or "unknown",
        "subtitle_type": "asr",
        "segments": segments,
        "full_text": full_text,
    }


# ===== 处理主流程 =====

def submit_processing(job_id: str):
    """把任务丢进串行线程池"""
    _executor.submit(_process_job, job_id)


def _is_cancelled(job_id: str) -> bool:
    """任务被 delete_job 移除、或服务正在停机，都视为应当中止"""
    if _shutting_down.is_set():
        return True
    with _jobs_lock:
        return job_id not in _jobs


def asr_ready() -> tuple[bool, str]:
    """探测语音识别是否真的可用（ffmpeg + faster-whisper 都要在）"""
    if not FFMPEG_BIN or not FFPROBE_BIN:
        return False, "服务器未安装 ffmpeg，无法处理本地视频"
    import importlib.util
    if importlib.util.find_spec("faster_whisper") is None:
        return False, "服务器未安装语音识别组件（faster-whisper）"
    return True, ""


def _discard_work_dir(job_id: str):
    work_dir = os.path.join(UPLOAD_DIR, job_id)
    if os.path.isdir(work_dir):
        shutil.rmtree(work_dir, ignore_errors=True)


def _process_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            _discard_work_dir(job_id)
            return
        video_path = job["video_path"]
        # 管理员不限时长
        max_duration = 0 if job.get("is_admin") else MAX_DURATION_SECONDS

    try:
        # 1) 探测真实媒体信息
        _update_job(job_id, status="probing", progress=5, stage_text="正在校验视频文件...")
        meta = probe_video(video_path, max_duration=max_duration)
        duration = meta["duration"]
        _update_job(
            job_id,
            duration=round(duration, 2),
            duration_string=_format_duration(duration),
        )

        if _is_cancelled(job_id):
            _discard_work_dir(job_id)
            return

        # 2) 提取音频
        _update_job(job_id, status="extracting", progress=15, stage_text="正在提取音频...")
        audio_path = os.path.splitext(video_path)[0] + ".wav"
        extract_audio(video_path, audio_path)
        _update_job(job_id, audio_path=audio_path)

        # 视频文件在音频提取完成后即可删除（不长期保存用户视频）
        try:
            os.remove(video_path)
            _update_job(job_id, video_path=None)
        except OSError:
            pass

        if _is_cancelled(job_id):
            _discard_work_dir(job_id)
            return

        # 3) 语音识别生成带时间戳字幕
        _update_job(job_id, status="transcribing", progress=20, stage_text="正在识别语音，生成字幕...")

        def on_progress(pct):
            _update_job(
                job_id,
                progress=max(20, min(99, 20 + int(pct * 0.79))),
                stage_text=f"正在识别语音，生成字幕... {pct}%",
            )

        subtitle = transcribe(audio_path, duration, on_progress=on_progress)

        # 4) 音频也不保留
        try:
            os.remove(audio_path)
            _update_job(job_id, audio_path=None)
        except OSError:
            pass

        # 转写期间用户可能已删除任务，此时结果直接丢弃
        if _is_cancelled(job_id):
            _discard_work_dir(job_id)
            return

        if not subtitle["has_subtitle"]:
            _update_job(
                job_id,
                status="failed",
                progress=100,
                stage_text="识别完成",
                error="未能从该视频中识别出任何语音内容，可能是纯音乐或无人声视频",
            )
            _discard_work_dir(job_id)
            return

        _update_job(
            job_id,
            status="done",
            progress=100,
            stage_text="处理完成",
            subtitle=subtitle,
        )

        # 媒体文件已删除，工作目录此时为空，一并回收
        _discard_work_dir(job_id)

    except LocalVideoError as e:
        _fail_job(job_id, str(e))
    except Exception as e:
        _fail_job(job_id, f"视频处理失败：{e}")


def _fail_job(job_id: str, message: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job:
        _cleanup_files(job)
    _discard_work_dir(job_id)
    _update_job(
        job_id,
        status="failed",
        progress=100,
        stage_text="处理失败",
        error=message,
        video_path=None,
        audio_path=None,
    )
