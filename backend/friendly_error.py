"""把 yt-dlp / 网络层的原始英文报错翻译成用户能看懂的中文提示。"""


def friendly_error(e: Exception) -> str:
    """将异常转换为面向用户的友好提示，匹配不到时返回原始信息。"""
    msg = str(e)

    rules = [
        ("HTTP Error 412", "平台暂时拒绝了请求，通常是解析引擎版本过旧。服务会在启动时自动升级，请重启后端或稍后重试。"),
        ("HTTP Error 403", "平台拒绝了访问，该视频可能需要登录、存在地区限制，或请求被平台风控拦截。"),
        ("HTTP Error 404", "视频不存在或已被删除。"),
        ("Video unavailable", "视频不存在或已被删除。"),
        ("Private video", "这是私密视频，无法解析。"),
        ("Unsupported URL", "暂不支持该平台的链接。"),
        ("Unable to extract", "平台页面结构可能已更新，解析规则暂时失效，请稍后重试。"),
        ("Requested format is not available", "所选清晰度不可用，请换一个格式重试。"),
        ("timed out", "网络超时，请检查网络连接后重试。"),
        ("Timeout", "网络超时，请检查网络连接后重试。"),
        ("Connection refused", "无法连接目标网站，请检查网络或代理设置。"),
        ("Sign in to confirm", "平台要求登录验证，该视频暂时无法解析。"),
        ("Failed to resolve", "无法访问该网站（域名解析失败），请检查链接是否正确，以及网络或代理设置。"),
        ("getaddrinfo failed", "无法访问该网站（域名解析失败），请检查链接是否正确，以及网络或代理设置。"),
    ]
    for keyword, tip in rules:
        if keyword in msg:
            return tip

    # 兜底：原始信息截断，避免整段堆栈直接弹给用户
    return msg[:300]
