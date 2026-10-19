/**
 * 站点级常量 —— 全站唯一一份「对外身份」。
 *
 * 为什么要有这个文件：canonical / og:url / sitemap / JSON-LD 里的域名必须**处处一致**，
 * 而 SPA 的 head 又是运行时拼的。散在各处手写域名，迟早会有一处漂成 http 或漏掉 www。
 *
 * ⚠️ 换域名要改 **4 处**（字面量，不是 import，改不动就是漏了）：
 *      1. 本文件的 `SITE_URL`（下面这行）
 *      2. `webui-frontend/index.html`      —— og:image + 两段 JSON-LD 的 url（3 处）
 *      3. `webui-frontend/public/robots.txt` —— Sitemap 行
 *      4. `webui-frontend/public/sitemap.xml` —— 两个 <loc>
 *    `scripts/check-site-url.mjs` 会在 build 前扫全仓比对这四者，
 *    不一致直接**构建失败**并指出文件与行号 —— 不要靠记性。
 *
 * ⚠️ 不在本清单里、也**不要跟着改**的域名：
 *    - `mpay.lizigege.com` 是支付网关自己的站点，与产品站不同域，改它会打不出二维码。
 *    - `docs.demo@lizigege.com` 是演示账号邮箱，不是访问地址。
 */
export const SITE_URL = "https://voice.lizigege.com";
export const SITE_NAME = "播客工坊";
export const SITE_NAME_EN = "Podcast Studio";
export const SITE_LANG = "zh-CN";
