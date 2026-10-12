<template>
  <div class="bg-white rounded-2xl border border-border shadow-lg overflow-hidden h-full">
    <div class="flex flex-col gap-5 p-5 sm:p-6">
      <!-- 本地视频没有平台缩略图，用占位图 + 时长角标 -->
      <div class="relative w-full aspect-video rounded-xl overflow-hidden bg-gradient-to-br from-primary-light to-gray-100 flex items-center justify-center">
        <svg class="w-14 h-14 text-primary opacity-40" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5"
            d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z" />
        </svg>
        <div v-if="video.duration_string" class="absolute bottom-2 right-2 px-2 py-0.5 bg-black/70 text-white text-xs rounded-md">
          {{ video.duration_string }}
        </div>
      </div>

      <div class="flex-1 min-w-0">
        <h3 class="text-lg font-semibold text-text-primary leading-snug mb-2 line-clamp-2">
          {{ video.title }}
        </h3>
        <div class="flex flex-wrap items-center gap-3 text-sm text-text-secondary">
          <span class="inline-flex items-center gap-1 px-2 py-0.5 bg-primary-light text-primary rounded-full text-xs font-medium">
            本地视频
          </span>
          <span v-if="video.filesize">{{ formatSize(video.filesize) }}</span>
          <span v-if="video.segment_count">{{ video.segment_count }} 条字幕</span>
        </div>
      </div>
    </div>

    <div class="border-t border-border-light px-5 sm:px-6 py-5">
      <div class="rounded-xl bg-blue-50 border border-blue-100 px-4 py-3 mb-5">
        <p class="text-sm text-blue-800 leading-relaxed">
          字幕由语音识别生成，可在右侧查看总结、思维导图，并导出 SRT / VTT / TXT 字幕。
        </p>
        <p class="text-xs text-blue-600 mt-1.5">
          原始视频已从服务器删除，仅保留文字结果
        </p>
      </div>

      <div class="flex flex-col items-stretch gap-3">
        <button
          @click="$emit('summarize')"
          :disabled="summarizing"
          class="w-full inline-flex items-center justify-center gap-2 h-12 px-8 rounded-full border-2 border-primary text-primary hover:bg-primary hover:text-white font-medium text-base transition-all shadow-sm hover:shadow-md cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:bg-transparent disabled:hover:text-primary"
        >
          <svg v-if="summarizing" class="animate-spin w-5 h-5" fill="none" viewBox="0 0 24 24">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
          </svg>
          <svg v-else class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
              d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
          </svg>
          {{ summarizing ? '总结中...' : '重新 AI 总结' }}
        </button>

        <button
          @click="$emit('delete')"
          class="w-full inline-flex items-center justify-center gap-2 h-11 px-8 rounded-full border border-border text-text-secondary hover:border-red-300 hover:text-red-600 hover:bg-red-50 font-medium text-sm transition-all cursor-pointer"
        >
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
              d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
          </svg>
          删除视频和分析结果
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
defineProps({
  video: { type: Object, required: true },
  summarizing: Boolean,
})
defineEmits(['summarize', 'delete'])

function formatSize(bytes) {
  if (!bytes) return ''
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)}KB`
  return `${(bytes / 1024 / 1024).toFixed(1)}MB`
}
</script>
