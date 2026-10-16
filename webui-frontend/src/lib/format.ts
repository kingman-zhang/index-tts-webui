/** 全站共用的展示层格式化。
 *
 *  为什么抽出来：`fmtYuan` 此前在 `AccountPage` 与 `PayDialog` 里各写了一份、
 *  内容逐字相同；再加一处「全部订单」页就是三份。金额格式一旦漂了不是风格问题，
 *  而是**显示错的价钱**。放这里改一次三处一起变。
 *
 *  ⚠️ 不抽 `Header.tsx` 里那个 `fmtTime`：它是 `MM-DD HH:mm` 的短格式，与这里的
 *  `YYYY-MM-DD HH:mm` 是**不同的展示需求**，不是重复。硬合成一个带开关的函数
 *  只会让两个调用点都变难读。
 */

/** 分 → `¥x.xx`；整元省掉小数（`¥10` 而不是 `¥10.00`），与后端 `price_yuan` 的观感一致。 */
export function fmtYuan(fen: number): string {
  return `¥${(fen / 100).toFixed(2).replace(/\.00$/, "")}`;
}

/**
 * ISO 时间串 → `YYYY-MM-DD HH:mm`（按浏览器本地时区渲染）。
 *
 * ⚠️ 后端下发的是**不带时区标记的裸串**（`datetime.now().isoformat()`），
 * `new Date(裸串)` 会按**浏览器本地时区**解析 —— 所以显示对不对，取决于
 * "写这行的那台机器"跑在哪个时区。生产容器已设 `TZ=Asia/Shanghai`；
 * 若页面时间整体偏 8 小时，去查进程时区，别改这里。
 */
export function fmtTime(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 合成语速，**字/秒**。用来把「字数」折算成「时长」给用户看。
 *
 *  来源＝本地历史 A/B 产物（`data/outputs/<实验名>/results.json`，44 条 ≥3 秒的「文本 + 秒数」配对）
 *  加权实测 **5.08 字/秒**（默认 speed=1.0）。短样本明显偏低（首尾静音与固定开销占比大），
 *  所以只取 ≥3 秒那一档。详见项目记忆 `队列与计费.md` 的「积分 ↔ 时长」换算条。
 *
 *  ⚠️ **这是写死在前端的**（2026-10-15 用户拍板：不新增后端下发字段）。代价要记住：
 *  改了 `MEMBER_POINTS_PER_1000_CHARS`（单价）或换引擎导致语速变化，这里**不会**跟着变；
 *  时长是按「同一份字数」现算的，所以至少不会和旁边的「N 万字」自相矛盾。
 *
 *  2026-10-16 从 `AccountPage.tsx` 提到这里：落地页也要用同一口径，
 *  再抄一份就等于给自己埋一个「两处时长对不上」的坑。 */
export const TTS_CHARS_PER_SECOND = 5.08;

/** 把字数折算成「N 小时 / N 分钟」（不含「约」，由调用处的 ≈ 承担）。
 *
 *  ⚠️ **只给量级，别写分钟级精度**：同一内容换读法（「230」读「二三零」vs「两百三十」）
 *  或改标点密度，实测时长能差 20–40%。用户按「65 分钟」去规划节目、做出来 50 分钟，
 *  会觉得被坑；「约 1 小时」既好记又抗波动。 */
export function fmtDuration(chars: number | null): string {
  if (!chars || chars <= 0) return "";
  const seconds = chars / TTS_CHARS_PER_SECOND;
  if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))} 分钟`;
  return `${Number((seconds / 3600).toFixed(1))} 小时`;
}
