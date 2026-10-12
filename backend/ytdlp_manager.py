"""yt-dlp 版本管理：启动时自动检查并升级，避免平台接口变更导致解析大面积失效。

注意：本模块不 import yt_dlp，版本号通过 importlib.metadata 读取，
以便在 yt_dlp 被加载进进程之前完成升级，升级后无需重启即可生效。
"""

import os
import subprocess
import sys
import time
from importlib import metadata

import httpx

PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"
PACKAGE_NAME = "yt-dlp"

# 最新版本号的缓存，避免健康检查等高频调用反复请求 PyPI
_latest_cache = {"version": None, "checked_at": 0.0}
_CACHE_TTL = 3600  # 秒


def get_installed_version() -> str:
    """当前环境安装的 yt-dlp 版本"""
    try:
        return metadata.version(PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return ""


def get_latest_version(force: bool = False) -> str:
    """PyPI 上的最新版本（带缓存，网络失败时返回空字符串）"""
    now = time.time()
    if not force and _latest_cache["version"] and now - _latest_cache["checked_at"] < _CACHE_TTL:
        return _latest_cache["version"]
    try:
        resp = httpx.get(PYPI_URL, timeout=5)
        resp.raise_for_status()
        version = resp.json()["info"]["version"]
        _latest_cache["version"] = version
        _latest_cache["checked_at"] = now
        return version
    except Exception:
        return ""


def _version_tuple(v: str) -> tuple:
    """'2026.08.19' -> (2026, 8, 19)，无法解析的片段按 0 处理"""
    parts = []
    for p in v.split("."):
        digits = "".join(c for c in p if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_outdated() -> bool:
    """当前版本是否落后于 PyPI 最新版本"""
    current, latest = get_installed_version(), get_latest_version()
    if not current or not latest:
        return False
    return _version_tuple(current) < _version_tuple(latest)


def upgrade() -> bool:
    """用 pip 升级 yt-dlp，成功返回 True"""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE_NAME],
            capture_output=True,
            text=True,
            timeout=300,
        )
        return result.returncode == 0
    except Exception:
        return False


def auto_upgrade_on_startup():
    """启动时检查并自动升级 yt-dlp。

    必须在任何 import yt_dlp 的代码之前调用，这样升级后进程内加载的就是新版。
    可通过环境变量 YTDLP_AUTO_UPGRADE=false 关闭。
    """
    if os.getenv("YTDLP_AUTO_UPGRADE", "true").lower() == "false":
        return

    current = get_installed_version()
    latest = get_latest_version(force=True)
    if not latest:
        print(f"[yt-dlp] 无法获取最新版本信息（网络问题？），当前版本 {current}，跳过检查")
        return
    if not current:
        print(f"[yt-dlp] 未安装，将自动安装最新版 {latest}")
    elif not is_outdated():
        print(f"[yt-dlp] 当前版本 {current} 已是最新")
        return
    else:
        print(f"[yt-dlp] 发现新版本 {latest}（当前 {current}），正在自动升级...")

    if upgrade():
        # 升级后清掉缓存，让后续读取到新版本号
        _latest_cache["version"] = None
        _latest_cache["checked_at"] = 0.0
        print(f"[yt-dlp] 升级完成，当前版本 {get_installed_version()}")
    else:
        print(f"[yt-dlp] 自动升级失败，仍使用当前版本 {current}，可手动执行: pip install --upgrade yt-dlp")
