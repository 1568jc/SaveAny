/**
 * 本地视频分析结果的浏览器缓存
 *
 * 解决的问题：刷新页面、关掉标签页、或退出后重新登录，结果就全没了。
 * 任务状态只存在服务端内存里，前端一旦丢失 job_id 就再也找不回来。
 *
 * 设计约束：
 * 1. 按账号隔离——缓存里记了 user_id，换账号登录看不到上一个人的内容。
 * 2. 保留 2 小时，与服务端任务 TTL 一致，避免「前端还在、后端早没了」的错位。
 * 3. 只缓存文字结果（字幕/总结/导图），不缓存视频本身。
 */

const KEY = 'saveany_local_result'
const TTL_MS = 2 * 60 * 60 * 1000 // 2 小时，与后端 JOB_TTL_SECONDS 对齐

function read() {
  try {
    const raw = localStorage.getItem(KEY)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

/**
 * 读取当前账号的缓存结果，过期或不属于该账号一律返回 null
 * @param {number|string} userId 当前登录用户的 id
 */
export function loadLocalResult(userId) {
  const data = read()
  if (!data || userId == null) return null

  if (String(data.user_id) !== String(userId)) return null

  if (!data.saved_at || Date.now() - data.saved_at > TTL_MS) {
    clearLocalResult()
    return null
  }
  if (!data.video || !data.video.job_id) return null

  return data
}

/**
 * 保存/更新当前账号的结果。summary 等字段可分批写入。
 */
export function saveLocalResult(userId, video, partial = {}) {
  if (userId == null || !video || !video.job_id) return

  const existing = read()
  const sameJob =
    existing &&
    String(existing.user_id) === String(userId) &&
    existing.video &&
    existing.video.job_id === video.job_id

  const data = {
    user_id: userId,
    saved_at: Date.now(),
    video,
    subtitle: sameJob ? existing.subtitle : null,
    summary: sameJob ? existing.summary : '',
    mindmap: sameJob ? existing.mindmap : '',
    quota: sameJob ? existing.quota : null,
    ...partial,
  }

  try {
    localStorage.setItem(KEY, JSON.stringify(data))
  } catch {
    // 超出配额（字幕过长）时退化为只存元信息，保证功能不因缓存失败而中断
    try {
      localStorage.setItem(
        KEY,
        JSON.stringify({ ...data, subtitle: null, summary: '', mindmap: '' })
      )
    } catch {
      /* 缓存不可用就算了，不影响正常使用 */
    }
  }
}

export function clearLocalResult() {
  try {
    localStorage.removeItem(KEY)
  } catch {
    /* ignore */
  }
}
