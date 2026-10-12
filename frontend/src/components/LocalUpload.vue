<template>
  <div class="max-w-2xl mx-auto">
    <!-- 未登录提示 -->
    <div
      v-if="!user"
      class="rounded-2xl border border-border bg-white shadow-sm px-6 py-8 text-center"
    >
      <svg class="w-10 h-10 mx-auto mb-3 text-text-muted opacity-50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
      </svg>
      <p class="text-text-primary font-medium mb-1">上传本地视频需要先登录</p>
      <p class="text-sm text-text-secondary mb-4">登录后即可上传视频，自动生成字幕并使用 AI 总结</p>
      <button
        @click="emit('need-login')"
        class="h-10 px-6 rounded-full bg-primary hover:bg-primary-dark text-white text-sm font-medium transition-all cursor-pointer"
      >
        立即登录
      </button>
    </div>

    <!-- 服务端未就绪：直接说明原因，不要让用户白传一遍 -->
    <div
      v-else-if="config.asr_available === false"
      class="rounded-2xl border border-orange-200 bg-orange-50 px-6 py-8 text-center"
    >
      <svg class="w-10 h-10 mx-auto mb-3 text-orange-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L4.082 16.5c-.77.833.192 2.5 1.732 2.5z" />
      </svg>
      <p class="text-orange-800 font-medium mb-1">本地视频解析暂不可用</p>
      <p class="text-sm text-orange-700">
        {{ config.asr_unavailable_reason || '服务器语音识别组件未就绪' }}，请联系管理员
      </p>
    </div>

    <!-- 上传区（空闲状态） -->
    <div
      v-else-if="phase === 'idle'"
      @dragover.prevent="dragging = true"
      @dragleave.prevent="dragging = false"
      @drop.prevent="onDrop"
      @click="triggerFilePick"
      :class="[
        'rounded-2xl border-2 border-dashed bg-white px-6 py-10 text-center cursor-pointer transition-all',
        dragging ? 'border-primary bg-primary-light/40' : 'border-border hover:border-primary/50 hover:bg-gray-50'
      ]"
    >
      <input
        ref="fileInput"
        type="file"
        :accept="acceptAttr"
        class="hidden"
        @change="onFileChange"
      />
      <svg class="w-12 h-12 mx-auto mb-4 text-primary opacity-70" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" />
      </svg>
      <p class="text-base font-medium text-text-primary mb-1.5">
        点击选择视频，或把文件拖到这里
      </p>
      <p class="text-sm text-text-secondary">
        支持 {{ formatListLabel }} · 最大 {{ sizeLabel }} · {{ durationLabel }}
      </p>
      <p v-if="config.is_admin" class="text-xs text-primary mt-2 font-medium">
        管理员账号：不受次数、并发与时长限制
      </p>
      <p class="text-xs text-text-muted mt-3">
        视频仅用于生成字幕，处理完成后会立即从服务器删除
      </p>
    </div>

    <!-- 上传 / 处理中 / 结果 -->
    <div v-else class="rounded-2xl border border-border bg-white shadow-sm px-5 sm:px-6 py-5 text-left">
      <!-- 文件信息头 -->
      <div class="flex items-start gap-3 mb-4">
        <div class="flex-shrink-0 w-10 h-10 rounded-xl bg-primary-light flex items-center justify-center">
          <svg class="w-5 h-5 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z" />
          </svg>
        </div>
        <div class="flex-1 min-w-0">
          <p class="text-sm font-medium text-text-primary truncate">{{ fileName }}</p>
          <p class="text-xs text-text-muted mt-0.5">
            {{ formatSize(fileSize) }}
            <span v-if="status && status.duration_string"> · {{ status.duration_string }}</span>
            <span v-if="status && status.segment_count"> · {{ status.segment_count }} 条字幕</span>
          </p>
        </div>
        <button
          @click="handleDelete"
          :disabled="deleting"
          class="flex-shrink-0 p-2 rounded-lg text-text-muted hover:text-red-500 hover:bg-red-50 transition-colors cursor-pointer disabled:opacity-50"
          title="删除视频和生成结果"
        >
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
          </svg>
        </button>
      </div>

      <!-- 失败 -->
      <div v-if="phase === 'error'" class="rounded-xl bg-red-50 border border-red-200 px-4 py-3">
        <div class="flex items-start gap-2.5">
          <svg class="w-5 h-5 text-red-500 flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L4.082 16.5c-.77.833.192 2.5 1.732 2.5z" />
          </svg>
          <div class="flex-1 min-w-0">
            <p class="text-sm font-medium text-red-800 mb-0.5">处理失败</p>
            <p class="text-sm text-red-700 break-words">{{ errorMessage }}</p>
          </div>
        </div>
        <button
          @click="reset"
          class="mt-3 h-9 px-4 rounded-lg bg-white border border-red-200 text-red-700 text-sm font-medium hover:bg-red-50 transition-colors cursor-pointer"
        >
          重新上传
        </button>
      </div>

      <!-- 完成 -->
      <div v-else-if="phase === 'done'" class="rounded-xl bg-green-50 border border-green-200 px-4 py-3 flex items-center gap-2.5">
        <svg class="w-5 h-5 text-green-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
        <p class="text-sm text-green-800">
          字幕已生成，AI 分析结果见右侧
        </p>
      </div>

      <!-- 进行中：上传 / 处理 -->
      <div v-else>
        <div class="flex items-center justify-between mb-2">
          <span class="text-sm text-text-secondary">{{ stageText }}</span>
          <span class="text-sm font-medium text-primary tabular-nums">{{ displayPercent }}%</span>
        </div>
        <div class="h-2 w-full rounded-full bg-gray-100 overflow-hidden">
          <div
            class="h-full rounded-full bg-primary transition-all duration-300"
            :style="{ width: displayPercent + '%' }"
          ></div>
        </div>
        <p class="text-xs text-text-muted mt-2.5">
          {{ phase === 'uploading'
            ? '正在上传，请勿关闭页面'
            : '语音识别较耗时，长视频请耐心等待' }}
        </p>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import {
  getLocalConfig,
  uploadLocalVideo,
  getLocalStatus,
  deleteLocalJob,
  extractErrorMessage,
} from '../api/local.js'

const props = defineProps({
  user: { type: Object, default: null },
})
const emit = defineEmits(['need-login', 'ready', 'reset'])

// idle -> uploading -> processing -> done | error
const phase = ref('idle')
const dragging = ref(false)
const fileInput = ref(null)
const fileName = ref('')
const fileSize = ref(0)
const uploadPercent = ref(0)
const jobId = ref('')
const status = ref(null)
const errorMessage = ref('')
const deleting = ref(false)

const config = ref({
  max_file_size: 500 * 1024 * 1024,
  max_file_size_mb: 500,
  max_duration_minutes: 60,
  allowed_extensions: ['.mp4', '.mov', '.mkv', '.avi', '.webm', '.flv', '.wmv', '.ts'],
})

// 支持的格式全部来自后端 /config，前后端不会出现两份不一致的清单
const acceptAttr = computed(
  () => (config.value.allowed_extensions || []).join(',') + ',video/*'
)

const sizeLabel = computed(() => {
  const mb = config.value.max_file_size_mb || 0
  return mb >= 1024 ? `${(mb / 1024).toFixed(mb % 1024 === 0 ? 0 : 1)}GB` : `${mb}MB`
})

// 后端用 0 表示不限时长（管理员）
const durationLabel = computed(() => {
  const min = config.value.max_duration_minutes
  return min ? `最长 ${min} 分钟` : '时长不限'
})

// 只展示常见的几种，避免文案过长
const formatListLabel = computed(() => {
  const preferred = ['.mp4', '.mov', '.mkv', '.avi', '.webm', '.flv', '.wmv', '.ts']
  const allowed = config.value.allowed_extensions || []
  const shown = preferred.filter((e) => allowed.includes(e))
  const list = (shown.length ? shown : allowed.slice(0, 8))
    .map((e) => e.replace('.', '').toUpperCase())
    .join(' / ')
  return list + ' 等格式'
})

let pollTimer = null
// 每次上传/重置都自增。异步回调回来时若 runId 已变，说明本轮已被
// 删除或重置，回调结果必须丢弃，否则会把过期状态写回界面。
let runId = 0

const stageText = computed(() => {
  if (phase.value === 'uploading') return '正在上传视频...'
  return status.value?.stage_text || '正在处理...'
})

const displayPercent = computed(() => {
  if (phase.value === 'uploading') return uploadPercent.value
  return status.value?.progress ?? 0
})

async function loadConfig() {
  try {
    config.value = await getLocalConfig()
  } catch {
    /* 用默认值兜底即可 */
  }
}

onMounted(loadConfig)

// 限额是按登录用户返回的（管理员更宽松），登录/退出后需要重新拉取
watch(() => props.user && props.user.id, loadConfig)

onBeforeUnmount(stopPolling)

function triggerFilePick() {
  fileInput.value?.click()
}

function onFileChange(e) {
  const file = e.target.files?.[0]
  if (file) startUpload(file)
  e.target.value = ''
}

function onDrop(e) {
  dragging.value = false
  const file = e.dataTransfer?.files?.[0]
  if (file) startUpload(file)
}

/** 前端先做一轮校验，尽早给出明确提示，避免白等一次大文件上传 */
function validate(file) {
  const name = file.name || ''
  const dot = name.lastIndexOf('.')
  const ext = dot >= 0 ? name.slice(dot).toLowerCase() : ''
  const allowed = config.value.allowed_extensions || []
  if (!ext || !allowed.includes(ext)) {
    const shown = ext ? ext.replace('.', '').toUpperCase() : '未知'
    return `暂不支持 ${shown} 格式，目前支持 ${formatListLabel.value}，请转换后再上传`
  }
  if (file.size > config.value.max_file_size) {
    return `文件过大（${formatSize(file.size)}），最大支持 ${config.value.max_file_size_mb}MB`
  }
  if (file.size === 0) {
    return '文件为空，请重新选择'
  }
  return null
}

async function startUpload(file) {
  const invalid = validate(file)
  if (invalid) {
    fileName.value = file.name
    fileSize.value = file.size
    errorMessage.value = invalid
    phase.value = 'error'
    return
  }

  const myRun = ++runId
  fileName.value = file.name
  fileSize.value = file.size
  uploadPercent.value = 0
  errorMessage.value = ''
  status.value = null
  phase.value = 'uploading'
  emit('reset')

  try {
    const data = await uploadLocalVideo(file, (p) => {
      if (myRun === runId) uploadPercent.value = p
    })
    if (myRun !== runId) return  // 上传期间已被重置/删除，丢弃结果
    jobId.value = data.job_id
    phase.value = 'processing'
    startPolling(myRun)
  } catch (err) {
    if (myRun !== runId) return
    errorMessage.value = extractErrorMessage(err, '上传失败，请稍后重试')
    phase.value = 'error'
  }
}

function startPolling(myRun) {
  stopPolling()
  pollTimer = setInterval(async () => {
    const id = jobId.value
    try {
      const s = await getLocalStatus(id)
      if (myRun !== runId || id !== jobId.value) return
      status.value = s
      if (s.status === 'done') {
        stopPolling()
        phase.value = 'done'
        emit('ready', {
          job_id: id,
          title: stripExt(fileName.value),
          duration: s.duration,
          duration_string: s.duration_string,
          filesize: s.filesize,
          segment_count: s.segment_count,
        })
      } else if (s.status === 'failed') {
        stopPolling()
        errorMessage.value = s.error || '视频处理失败'
        phase.value = 'error'
      }
    } catch (err) {
      if (myRun !== runId) return
      stopPolling()
      errorMessage.value = extractErrorMessage(err, '获取处理状态失败')
      phase.value = 'error'
    }
  }, 1500)
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function handleDelete() {
  if (!confirm('确定删除这个视频及其生成的字幕和分析结果吗？')) return
  deleting.value = true
  stopPolling()
  try {
    if (jobId.value) await deleteLocalJob(jobId.value)
  } catch {
    /* 任务可能已过期清理，忽略即可 */
  } finally {
    deleting.value = false
    reset()
  }
}

function reset() {
  runId++          // 作废所有在飞的上传/轮询回调
  stopPolling()
  phase.value = 'idle'
  jobId.value = ''
  status.value = null
  errorMessage.value = ''
  uploadPercent.value = 0
  fileName.value = ''
  fileSize.value = 0
  emit('reset')
}

function stripExt(name) {
  const i = name.lastIndexOf('.')
  return i > 0 ? name.slice(0, i) : name
}

function formatSize(bytes) {
  if (!bytes) return '0MB'
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)}KB`
  return `${(bytes / 1024 / 1024).toFixed(1)}MB`
}

defineExpose({ reset })
</script>
