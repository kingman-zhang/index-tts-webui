/**
 * 站点级常量 —— 全站唯一一份「对外身份」。
 *
 * 为什么要有这个文件：canonical / og:url / sitemap / JSON-LD 里的域名必须**处处一致**，
 * 而 SPA 的 head 又是运行时拼的。散在各处手写域名，迟早会有一处漂成 http 或漏掉 www。
 *
 * ⚠️ 换域名只改这里 + `webui-frontend/public/robots.txt`、`public/sitemap.xml`
 *    （那两个是纯静态文件，构建期不参与，无法自动同步）。
 */
export const SITE_URL = "https://media.lizigege.com";
export const SITE_NAME = "播客工坊";
export const SITE_NAME_EN = "Podcast Studio";
export const SITE_LANG = "zh-CN";
