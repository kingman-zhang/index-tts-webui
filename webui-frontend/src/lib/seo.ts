/**
 * 页面级 SEO：在客户端路由切换后，把 title / description / canonical / OG / JSON-LD
 * 改成**当前页**的值。
 *
 * 为什么需要运行时改：本站是 SPA，`index.html` 只有一份静态 head。爬虫执行 JS 之后
 * 看到的是渲染结果，若 head 不跟着变，`/docs` 与 `/` 会共用同一个 title —— 等于
 * 自己跟自己抢词。构建期静态产物（`dist/landing.html`）已经写死了落地页的 head，
 * 这个模块负责的是「其余路由 + 开发环境 + landing.html 缺失时的回落路径」。
 *
 * 约定：以 `data-page-seo` 标记本模块写入的标签，每次先清后写，不留上一次的残留。
 */
import { useEffect } from "react";
import { SITE_URL } from "./site";

export interface PageSeo {
  title: string;
  description: string;
  /** 以 / 开头的路径；canonical 与 og:url 都由 SITE_URL + 它拼出 */
  canonicalPath: string;
  keywords?: string[];
  /** 绝对地址；缺省用站点默认分享图 */
  image?: string;
  jsonLd?: unknown[];
  /** 工作页（需登录）不希望被收录时置 true */
  noindex?: boolean;
}

const DEFAULT_OG_IMAGE = `${SITE_URL}/landing-assets/og-cover.jpg`;

function setMeta(attr: "name" | "property", key: string, content: string) {
  let el = document.head.querySelector<HTMLMetaElement>(`meta[${attr}="${key}"]`);
  if (!el) {
    el = document.createElement("meta");
    el.setAttribute(attr, key);
    document.head.appendChild(el);
  }
  el.setAttribute("content", content);
}

function setLink(rel: string, href: string) {
  let el = document.head.querySelector<HTMLLinkElement>(`link[rel="${rel}"]`);
  if (!el) {
    el = document.createElement("link");
    el.setAttribute("rel", rel);
    document.head.appendChild(el);
  }
  el.setAttribute("href", href);
}

export function applyPageSeo(seo: PageSeo) {
  const url = SITE_URL + seo.canonicalPath;
  const image = seo.image ?? DEFAULT_OG_IMAGE;

  document.title = seo.title;
  setMeta("name", "description", seo.description);
  if (seo.keywords?.length) setMeta("name", "keywords", seo.keywords.join(","));
  setMeta("name", "robots", seo.noindex ? "noindex,nofollow" : "index,follow");
  setLink("canonical", url);

  setMeta("property", "og:type", "website");
  setMeta("property", "og:site_name", "播客工坊");
  setMeta("property", "og:title", seo.title);
  setMeta("property", "og:description", seo.description);
  setMeta("property", "og:url", url);
  setMeta("property", "og:image", image);
  setMeta("name", "twitter:card", "summary_large_image");
  setMeta("name", "twitter:title", seo.title);
  setMeta("name", "twitter:description", seo.description);
  setMeta("name", "twitter:image", image);

  // JSON-LD：先清掉本模块上一轮写入的，再写当前页的
  document.head.querySelectorAll('script[data-page-seo="jsonld"]').forEach(n => n.remove());
  (seo.jsonLd ?? []).forEach(obj => {
    const s = document.createElement("script");
    s.type = "application/ld+json";
    s.setAttribute("data-page-seo", "jsonld");
    s.textContent = JSON.stringify(obj);
    document.head.appendChild(s);
  });
}

/**
 * 组件挂载时写入当页 SEO。
 *
 * ⚠️ 传进来的 `seo` 请用**模块级常量**（引用稳定）。对象字面量每次渲染都是新引用，
 * 会让 effect 反复执行 —— 不做深比较是刻意的：这里没有「同一页 SEO 变了要重算」的场景。
 */
export function usePageSeo(seo: PageSeo) {
  useEffect(() => {
    applyPageSeo(seo);
  }, [seo]);
}
