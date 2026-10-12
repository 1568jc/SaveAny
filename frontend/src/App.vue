<template>
  <div class="min-h-screen flex flex-col bg-bg-main">
    <AppHeader
      :user="currentUser"
      @login="showAuthModal('login')"
      @register="showAuthModal('register')"
      @logout="handleLogout"
      @open-vip="handleOpenVip"
    />
    <main class="flex-1">
      <HeroSection
        ref="heroRef"
        @parse="handleParse"
        :loading="loading"
        :compact="hasResult"
        :showSlogan="!hasResult || demoMode"
        :user="currentUser"
        @need-login="showAuthModal('login')"
        @local-ready="handleLocalReady"
        @local-reset="handleLocalReset"
        @source-change="handleSourceChange"
      />
      <!-- 视频信息 + AI 总结：左右双栏同屏布局 -->
      <section v-if="hasResult" class="py-4 sm:py-6 bg-white">
        <div class="max-w-7xl mx-auto px-4 sm:px-6">
          <div class="flex flex-col lg:flex-row gap-6">
            <!-- 左栏：视频信息（在线解析 / 本地上传） -->
            <div class="w-full lg:w-2/5 lg:flex-shrink-0">
              <VideoResult
                v-if="videoData"
                :video="videoData"
                :downloading="downloading"
                :summarizing="summarizing"
                @download="handleDownload"
                @summarize="handleSummarize"
              />
              <LocalVideoResult
                v-else-if="localVideo"
                :video="localVideo"
                :summarizing="summarizing"
                @summarize="handleSummarize"
                @delete="handleLocalDelete"
              />
            </div>
            <!-- 右栏：AI 总结（在线 / 本地共用同一个组件） -->
            <div class="w-full lg:w-3/5 min-w-0">
              <VideoSummary
                :videoUrl="videoData ? currentUrl : ''"
                :directUrl="videoData ? (videoData.formats?.find(f => f._direct_url)?._direct_url || '') : ''"
                :videoTitle="videoData ? videoData.title : localVideo.title"
                :user="currentUser"
                :mode="videoData ? 'online' : 'local'"
                :jobId="videoData ? '' : localVideo.job_id"
                :initialData="videoData ? null : restoredResult"
                :key="summaryKey"
                @loading-change="handleSummarizeLoadingChange"
                @need-login="showAuthModal('login')"
                @need-vip="handleOpenVip"
                @result="handleSummaryResult"
              />
            </div>
          </div>
        </div>
      </section>
      <FeatureSection />
      <HowToSection />
      <ComparisonSection />
      <PricingSection :user="currentUser" @open-vip="handleOpenVip" @need-login="showAuthModal('login')" />
      <PlatformSection />
    </main>
    <AppFooter />

    <!-- 支付成功/取消提示 -->
    <Teleport to="body">
      <div v-if="paymentToast" class="fixed top-20 left-1/2 -translate-x-1/2 z-[200] animate-toast-in">
        <div :class="[
          'flex items-center gap-3 px-6 py-4 rounded-2xl shadow-xl border',
          paymentToast === 'success' ? 'bg-green-50 border-green-200 text-green-800' : 'bg-orange-50 border-orange-200 text-orange-800'
        ]">
          <svg v-if="paymentToast === 'success'" class="w-5 h-5 text-green-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <svg v-else class="w-5 h-5 text-orange-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L4.082 16.5c-.77.833.192 2.5 1.732 2.5z" />
          </svg>
          <span class="font-medium text-sm">
            {{ paymentToast === 'success' ? 'VIP 开通成功！已为你激活全部高级功能' : '支付已取消，你可以随时再次开通' }}
          </span>
        </div>
      </div>
    </Teleport>

    <AuthModal
      :visible="authModalVisible"
      :initialMode="authModalMode"
      @close="authModalVisible = false"
      @success="handleAuthSuccess"
    />
  </div>
</template>

<script setup>
import { ref, computed, nextTick, onMounted, onBeforeUnmount } from 'vue'
import AppHeader from './components/AppHeader.vue'
import HeroSection from './components/HeroSection.vue'
import VideoResult from './components/VideoResult.vue'
import LocalVideoResult from './components/LocalVideoResult.vue'
import VideoSummary from './components/VideoSummary.vue'
import FeatureSection from './components/FeatureSection.vue'
import HowToSection from './components/HowToSection.vue'
import ComparisonSection from './components/ComparisonSection.vue'
import PricingSection from './components/PricingSection.vue'
import PlatformSection from './components/PlatformSection.vue'
import AppFooter from './components/AppFooter.vue'
import AuthModal from './components/AuthModal.vue'
import { parseVideo, downloadViaServer } from './api/video.js'
import { getSavedUser, fetchMe, logout as logoutApi, isLoggedIn } from './api/auth.js'
import { createCheckoutSession } from './api/payment.js'
import { deleteLocalJob } from './api/local.js'
import { loadLocalResult, saveLocalResult, clearLocalResult } from './api/localCache.js'

const demoMode = ref(false)
let enterCount = 0
let enterTimer = null

function onKeyDown(e) {
  if (e.key === 'Enter' && !e.target.matches('input, textarea, [contenteditable]')) {
    enterCount++
    clearTimeout(enterTimer)
    if (enterCount >= 3) {
      demoMode.value = !demoMode.value
      enterCount = 0
    } else {
      enterTimer = setTimeout(() => { enterCount = 0 }, 800)
    }
  }
}

onMounted(() => {
  document.addEventListener('keydown', onKeyDown)
  restoreUser()
  checkPaymentResult()
})
onBeforeUnmount(() => { document.removeEventListener('keydown', onKeyDown) })

// ===== 用户状态管理 =====
const currentUser = ref(null)
const authModalVisible = ref(false)
const authModalMode = ref('login')

function showAuthModal(mode = 'login') {
  authModalMode.value = mode
  authModalVisible.value = true
}

function handleAuthSuccess(user) {
  currentUser.value = user
  restoreLocalResult()   // 登录后找回该账号上次的本地视频结果
}

function handleLogout() {
  logoutApi()
  currentUser.value = null
  // 页面上必须清掉（否则下一个登录的账号会看到上一个账号的内容），
  // 但缓存保留：同一账号重新登录后还能找回自己的结果。
  // 缓存按 user_id 隔离，换账号登录读不到。
  clearLocal({ keepCache: true })
}

async function restoreUser() {
  if (!isLoggedIn()) return
  const saved = getSavedUser()
  if (saved) currentUser.value = saved
  try {
    currentUser.value = await fetchMe()
  } catch {
    handleLogout()
    return
  }
  restoreLocalResult()   // 刷新页面后找回上次的本地视频结果
}

// ===== VIP 购买 =====
async function handleOpenVip() {
  if (!isLoggedIn()) {
    showAuthModal('login')
    return
  }
  try {
    const { checkout_url } = await createCheckoutSession('monthly')
    window.location.href = checkout_url
  } catch (err) {
    const msg = err.response?.data?.detail || err.message || '创建支付失败'
    alert(typeof msg === 'string' ? msg : JSON.stringify(msg))
  }
}

// ===== 支付结果处理 =====
const paymentToast = ref(null)

function checkPaymentResult() {
  const params = new URLSearchParams(window.location.search)
  const payment = params.get('payment')
  if (payment === 'success' || payment === 'cancel') {
    paymentToast.value = payment
    window.history.replaceState({}, '', window.location.pathname)
    if (payment === 'success' && isLoggedIn()) {
      setTimeout(async () => {
        try { currentUser.value = await fetchMe() } catch {}
      }, 1000)
    }
    setTimeout(() => { paymentToast.value = null }, 5000)
  }
}

// ===== 视频功能 =====
const loading = ref(false)
const downloading = ref(false)
const videoData = ref(null)
const currentUrl = ref('')
const summaryKey = ref(0)
const summarizing = ref(false)

// ===== 本地上传视频（新增来源，与在线解析互斥） =====
const localVideo = ref(null)
const heroRef = ref(null)
// 从浏览器缓存恢复的结果，传给 VideoSummary 直接展示（不重新调用 AI）
const restoredResult = ref(null)

/**
 * 恢复上次的本地视频结果。
 * 结果只存在内存里，刷新页面或退出重进就全丢了——这里从 localStorage 找回来。
 * 缓存按账号隔离且 2 小时过期，换账号登录不会看到别人的内容。
 */
function restoreLocalResult() {
  if (!currentUser.value) return
  const cached = loadLocalResult(currentUser.value.id)
  if (!cached) return

  videoData.value = null
  currentUrl.value = ''
  restoredResult.value = {
    subtitle: cached.subtitle,
    summary: cached.summary,
    mindmap: cached.mindmap,
    quota: cached.quota,
  }
  localVideo.value = cached.video
  summaryKey.value++
  // 把来源切到「本地上传」，否则会出现结果在下面、上面却停在链接输入框
  nextTick(() => heroRef.value?.setSource('local'))
}

/** VideoSummary 产出新结果时写入缓存 */
function handleSummaryResult(payload) {
  if (!currentUser.value || !localVideo.value) return
  saveLocalResult(currentUser.value.id, localVideo.value, payload)
}

const hasResult = computed(() => !!videoData.value || !!localVideo.value)

/**
 * 清空本地视频来源的全部状态。
 * 必须同时重置 App 侧的 localVideo 和 HeroSection 内 LocalUpload 的内部状态——
 * LocalUpload 是 v-show 挂载的，切 Tab 不会卸载，只清一边会残留
 * “字幕已生成，AI 分析结果见右侧”而右侧已空的矛盾画面。
 */
function clearLocal({ keepCache = false } = {}) {
  localVideo.value = null
  restoredResult.value = null
  heroRef.value?.resetLocal()
  if (!keepCache) clearLocalResult()
}

function handleLocalReady(payload) {
  // 本地视频就绪：清掉在线解析结果，避免两种来源同时展示
  videoData.value = null
  currentUrl.value = ''
  restoredResult.value = null      // 新任务要真正跑一遍，不能复用旧缓存
  localVideo.value = payload
  clearLocalResult()
  if (currentUser.value) saveLocalResult(currentUser.value.id, payload)
  summaryKey.value++
}

function handleLocalReset() {
  // 来自 LocalUpload 的反向通知，此处只清 App 侧，避免与 clearLocal 相互递归
  localVideo.value = null
}

function handleSourceChange(key) {
  // 切换来源只清当前展示，缓存保留：切回「本地上传」时把上次结果找回来
  videoData.value = null
  currentUrl.value = ''
  clearLocal({ keepCache: true })
  if (key === 'local') restoreLocalResult()
}

async function handleLocalDelete() {
  if (!localVideo.value) return
  if (!confirm('确定删除这个视频及其生成的字幕和分析结果吗？')) return
  const jobId = localVideo.value.job_id
  clearLocal()
  try {
    await deleteLocalJob(jobId)
  } catch {
    /* 任务可能已过期被清理，忽略 */
  }
}

function handleSummarize() {
  // 用户主动点「重新 AI 总结」：丢掉缓存结果，真正重跑一次
  restoredResult.value = null
  summaryKey.value++
}

function handleSummarizeLoadingChange(isLoading) {
  summarizing.value = isLoading
}

async function handleParse(url) {
  loading.value = true
  videoData.value = null
  clearLocal({ keepCache: true })   // 解析在线链接不该抹掉本地视频的结果
  currentUrl.value = url
  try {
    const res = await parseVideo(url)
    if (res.success) {
      videoData.value = res.data
      summaryKey.value++
    } else {
      alert('解析失败：' + (res.error || '未知错误'))
    }
  } catch (err) {
    const msg = err.response?.data?.detail?.error || err.response?.data?.detail || err.message
    alert('解析失败：' + msg)
  } finally {
    loading.value = false
  }
}

async function handleDownload(formatId) {
  downloading.value = true
  try {
    const response = await downloadViaServer(currentUrl.value, formatId)
    const contentDisposition = response.headers['content-disposition']
    let filename = 'video.mp4'
    if (contentDisposition) {
      const match = contentDisposition.match(/filename\*?=(?:UTF-8'')?([^;\n]+)/i)
      if (match) filename = decodeURIComponent(match[1].replace(/"/g, ''))
    }
    const blob = new Blob([response.data])
    const url = window.URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
    window.URL.revokeObjectURL(url)
  } catch (err) {
    alert('下载失败：' + (err.message || '请稍后重试'))
  } finally {
    downloading.value = false
  }
}
</script>

<style>
@keyframes toast-in {
  from { opacity: 0; transform: translate(-50%, -10px); }
  to { opacity: 1; transform: translate(-50%, 0); }
}
.animate-toast-in {
  animation: toast-in 0.3s ease-out;
}
</style>
