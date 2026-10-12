"""管理员身份判定（基于环境变量，不改数据库、不改注册登录流程）

在 .env 里配置：
    ADMIN_EMAILS=you@example.com,teammate@example.com

命中的账号在使用本产品时不受以下限制：
  - 每日 AI 总结次数
  - 单用户同时处理任务数
  - 上传文件大小 / 视频时长

注意：这里只放宽「管理员自己」的用量限制，
不提供查看或管理其他用户数据的能力——任务归属校验依然对管理员生效。
"""

import os

# 管理员的放宽额度（仍保留有限上限，避免误操作把磁盘写满）
ADMIN_MAX_FILE_SIZE = int(os.getenv("ADMIN_MAX_FILE_SIZE_MB", "5000")) * 1024 * 1024


def _admin_emails() -> set[str]:
    """每次读取环境变量，改完配置重启即可生效"""
    raw = os.getenv("ADMIN_EMAILS", "") or ""
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def is_admin_email(email: str | None) -> bool:
    if not email:
        return False
    return email.strip().lower() in _admin_emails()


def is_admin(user: dict | None) -> bool:
    """user 为 get_current_user / get_optional_user 返回的用户字典"""
    if not user:
        return False
    return is_admin_email(user.get("email"))


def admin_configured() -> bool:
    return bool(_admin_emails())
