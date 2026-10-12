/**
 * 本地视频上传解析 API 封装
 *
 * 说明：AI 总结的 SSE 事件流与在线视频完全一致（subtitle/summary/mindmap/quota/done/error），
 * 因此直接复用 summarize.js 里已有的 SSE 解析器，不重复实现一套。
 * AI 问答则完全复用现有的 chatWithVideo（传 subtitle_text 即可）。
 */

import axios from 'axios'
import { getToken } from './auth'
import { handleSSEStream } from './summarize'

const api = axios.create({ baseURL: '/api/local' })

function authHeaders(extra = {}) {
  const token = getToken()
  const headers = { ...extra }
  if (token) headers['Authorization'] = `Bearer ${token}`
  return headers
}

/** 获取上传限制配置（大小/时长/格式）
 *  带上登录态，后端会返回该用户实际生效的限额（管理员更宽松） */
export async function getLocalConfig() {
  const { data } = await api.get('/config', { headers: authHeaders() })
  return data.data
}

/**
 * 上传本地视频文件
 * @param {File} file
 * @param {(percent:number)=>void} onProgress 上传进度回调 0-100
 */
export async function uploadLocalVideo(file, onProgress) {
  const form = new FormData()
  form.append('file', file)

  const { data } = await api.post('/upload', form, {
    headers: authHeaders(),
    timeout: 30 * 60 * 1000,
    onUploadProgress: (e) => {
      if (onProgress && e.total) {
        onProgress(Math.round((e.loaded / e.total) * 100))
      }
    },
  })
  return data.data
}

/** 查询处理状态 */
export async function getLocalStatus(jobId) {
  const { data } = await api.get(`/status/${jobId}`, { headers: authHeaders() })
  return data.data
}

/** 删除上传的视频及其生成结果 */
export async function deleteLocalJob(jobId) {
  const { data } = await api.delete(`/job/${jobId}`, { headers: authHeaders() })
  return data.data
}

/**
 * 本地视频 AI 总结（SSE 流式）
 * 事件与 /api/summarize 完全一致，前端组件可原样复用
 */
export async function summarizeLocalVideo(jobId, language = 'zh', callbacks = {}) {
  const token = getToken()
  const headers = { 'Content-Type': 'application/json' }
  if (token) headers['Authorization'] = `Bearer ${token}`

  const response = await fetch('/api/local/summarize', {
    method: 'POST',
    headers,
    body: JSON.stringify({ job_id: jobId, language }),
  })

  if (!response.ok) {
    throw await toReadableError(response, '总结请求失败')
  }

  await handleSSEStream(response, callbacks)
}

// HTTP 状态码兜底提示，保证任何情况下用户都看到中文
const STATUS_MESSAGES = {
  401: '登录已过期，请重新登录后再试',
  403: '没有权限执行该操作',
  404: '任务不存在或已被清理，请重新上传',
  413: '文件过大，请上传更小的视频',
  429: '你有视频正在处理中，请等它完成后再上传新的视频',
  500: '服务器处理出错，请稍后重试',
  502: '服务暂时不可用，请稍后重试',
  503: '服务暂时不可用，请稍后重试',
  504: '服务器响应超时，请稍后重试',
}

/** 从后端错误响应里取出可读的中文提示 */
export function extractErrorMessage(err, fallback = '操作失败') {
  const detail = err?.response?.data?.detail

  if (typeof detail === 'string') return detail
  if (detail?.error) return detail.error
  // FastAPI 参数校验错误是数组形式
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0]
    if (typeof first === 'string') return first
    if (first?.msg) return first.msg
  }

  const status = err?.response?.status
  if (status && STATUS_MESSAGES[status]) return STATUS_MESSAGES[status]

  // 网络层错误（断网、超时）的 err.message 是英文，换成中文
  if (err?.code === 'ECONNABORTED') return '请求超时，请检查网络后重试'
  if (err?.message === 'Network Error') return '网络连接失败，请检查网络后重试'
  if (err?.message) return err.message
  return fallback
}

/** 把 fetch 的非 2xx 响应转成带中文提示的 Error */
async function toReadableError(response, fallback) {
  let detail = null
  try {
    detail = (await response.json())?.detail
  } catch {
    /* 响应不是 JSON，走状态码兜底 */
  }
  const err = new Error(fallback)
  err.response = { status: response.status, data: { detail } }
  return new Error(extractErrorMessage(err, fallback))
}
