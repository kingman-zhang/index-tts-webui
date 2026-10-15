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
