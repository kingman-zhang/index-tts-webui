/**
 * 落地页的结构化数据（JSON-LD）。
 *
 * 只声明**页面上真实可见**的内容：SoftwareApplication 的能力清单对应页面「核心能力」区，
 * FAQPage 的问答逐条对应页面 FAQ 区（同一份 `FAQ_ITEMS`）。结构化数据与可见正文不一致
 * 属于误导性标记 —— 所以这里不做「为了吃富摘要而多写几条」的事，
 * 也不写 `aggregateRating`：本站没有真实评分数据，编一个就是伪造。
 */
import { SITE_URL, SITE_NAME, SITE_NAME_EN, SITE_LANG } from "@/lib/site";
import {
  FAQ_ITEMS, LANDING_DESCRIPTION, LANDING_OG_IMAGE, POINTS_PACKS,
  REG_BONUS_POINTS, VOICE_TOTAL_COUNT, EMOTION_LABELS, GLOSSARY_BUILTIN_COUNT, CHAPTER_MAX_CHARS,
} from "./content";

/** 页面「核心能力」区的能力清单（与正文逐条对应）。 */
export const FEATURE_LIST = [
  `${VOICE_TOTAL_COUNT} 条音色，可按性别 / 年龄 / 分类筛选并试听`,
  `上传 10 秒参考音频，用自己的声音配音`,
  `${EMOTION_LABELS.length} 种情绪标记与 0.05~5 秒停顿控制`,
  `书稿模式：整本按章连载，单章上限默认 ${CHAPTER_MAX_CHARS / 10000} 万字`,
  `术语词汇表：${GLOSSARY_BUILTIN_COUNT} 条内置纠音词条，专治多音字读错`,
  "单人配音与双人播客两套工作台，任务队列可排序、暂停、试听、下载",
];

export function landingJsonLd(): unknown[] {
  const url = SITE_URL + "/";
  return [
    {
      "@context": "https://schema.org",
      "@type": "Organization",
      name: SITE_NAME,
      alternateName: SITE_NAME_EN,
      url,
      description: LANDING_DESCRIPTION,
      inLanguage: SITE_LANG,
    },
    {
      "@context": "https://schema.org",
      "@type": "WebSite",
      name: SITE_NAME,
      url,
      inLanguage: SITE_LANG,
      description: LANDING_DESCRIPTION,
    },
    {
      "@context": "https://schema.org",
      "@type": "SoftwareApplication",
      name: SITE_NAME,
      alternateName: `${SITE_NAME} ${SITE_NAME_EN}`,
      applicationCategory: "MultimediaApplication",
      applicationSubCategory: "Text-to-Speech",
      operatingSystem: "Web",
      url,
      image: LANDING_OG_IMAGE,
      description: LANDING_DESCRIPTION,
      inLanguage: SITE_LANG,
      featureList: FEATURE_LIST,
      offers: [
        {
          "@type": "Offer",
          name: `注册赠送 ${REG_BONUS_POINTS} 积分`,
          price: "0",
          priceCurrency: "CNY",
          availability: "https://schema.org/InStock",
        },
        ...POINTS_PACKS.map(p => ({
          "@type": "Offer",
          name: `${p.name} ${p.points} 积分`,
          price: String(p.priceFen / 100),
          priceCurrency: "CNY",
          availability: "https://schema.org/InStock",
        })),
      ],
    },
    {
      "@context": "https://schema.org",
      "@type": "FAQPage",
      mainEntity: FAQ_ITEMS.map(f => ({
        "@type": "Question",
        name: f.q,
        acceptedAnswer: { "@type": "Answer", text: f.a },
      })),
    },
  ];
}
