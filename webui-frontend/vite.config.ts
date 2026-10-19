import { defineConfig, loadEnv } from 'vite'
import type { Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

/**
 * umami 统计注入（构建期）。
 *
 * ## 为什么用插件而不是直接写死 <script>
 *
 * 1. **可开关**：没配 `VITE_UMAMI_SRC` / `VITE_UMAMI_ID` 时完全不注入，
 *    本地开发、离线构建都不会往统计服务发请求（也不会出现坏标签）。
 * 2. **一处接入，两份文档都覆盖**：生产的 `/` 是 `dist/landing.html`，
 *    它的 head 由 `scripts/prerender.mjs` **手工拼**、不复用 `index.html` 的 head。
 *    在 index.html 里加标签、落地页却漏掉，是这个项目里已经出现过的坑。
 *    这里统一在 index.html 注入并打上 `umami:start/end` 标记，
 *    再由 prerender.mjs 按标记抽取同段插入 landing.html —— 单一接入点。
 *
 * ## 为什么带 <head> 而不是放 body 末尾
 *
 * umami 的 tracker 默认开启 path change 检测，能在 SPA 路由变化时补发 pageview。
 * 放 head 里 `defer` 加载，首屏即可用；埋点不会漏掉「进来就点登录」这种路径。
 *
 * ## 配置方式（webui-frontend/.env.production）
 *
 *     VITE_UMAMI_SRC=https://stats.lizigege.com/script.js
 *     VITE_UMAMI_ID=<在 umami 后台「添加网站」后拿到的 website-id>
 *     VITE_UMAMI_DOMAINS=voice.lizigege.com
 *
 * 最后一项用来把开发/预发环境的流量排除掉（tracker 拿它比对 window.location.hostname）。
 */
function umamiPlugin(env: Record<string, string>): Plugin {
  return {
    name: 'umami-inject',
    transformIndexHtml(html) {
      const src = env.VITE_UMAMI_SRC?.trim()
      const id = env.VITE_UMAMI_ID?.trim()
      if (!src || !id) return html // 未配置：不注入任何东西

      const domains = env.VITE_UMAMI_DOMAINS?.trim()
      const attrs = [
        'defer',
        `src="${src}"`,
        `data-website-id="${id}"`,
        domains ? `data-domains="${domains}"` : '',
        // Core Web Vitals：免费拿到真实用户的首屏耗时，用来判断落地页要不要优化
        'data-performance="true"',
      ]
        .filter(Boolean)
        .join(' ')

      const block = [
        '<!-- umami:start —— 由 vite.config.ts 的 umamiPlugin 注入；prerender.mjs 按此标记复制到 landing.html -->',
        `    <script ${attrs}></script>`,
        '    <!-- umami:end -->',
      ].join('\n')

      return html.replace('</head>', `  ${block}\n  </head>`)
    },
  }
}

export default defineConfig(({ mode }) => {
  // 只读 VITE_ 前缀的变量（vite 的惯例），顺带避免把无关的环境变量卷进来
  const env = loadEnv(mode, __dirname, 'VITE_')

  return {
    plugins: [react(), umamiPlugin(env)],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
      },
    },
    server: {
      host: '0.0.0.0',
      port: 6008,
      allowedHosts: [
        '.seetacloud.com' // 放行这个域名
      ],
      proxy: {
        '/api': {
          target: 'http://localhost:3001',
          changeOrigin: true,
        },
      },
    },
  }
})
