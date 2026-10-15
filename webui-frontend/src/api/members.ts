/** 会员/积分 API 客户端。 */
import { authFetch } from "@/lib/auth";
import type { MemberUser } from "@/lib/auth";

export interface PointLog {
  id: string;
  user_id: string;
  delta: number;
  balance_after: number;
  kind: "earn" | "spend" | "redeem" | "checkin" | "grant" | "refund" | "purchase";
  reason: string;
  ref: string;
  created_at: string;
}

/** 积分商城套餐（后端 packs.py 的镜像；入账只认 points，bonus_* 仅供展示）。 */
export interface PointsPack {
  id: string;
  name: string;
  price_fen: number;
  price_yuan: string;
  points: number;
  bonus_points: number;
  bonus_percent: number;
  /** 按当前计费单价折算的可合成字数；未开启按量计费时为 null */
  est_chars: number | null;
  order: number;
}

export interface PointsPackList {
  packs: PointsPack[];
  currency: string;
  points_per_yuan: number;
  points_per_1000_chars: number;
  /** true 时前端才显示「立即购买（模拟支付）」；接入真实支付后后端会置 false */
  mock_pay_enabled: boolean;
  /** 真实支付通道是否**已就绪**（当前通道已注册且配置齐全） */
  pay_channel_ready: boolean;
  /** 当前生效的通道 id（"" = 未接真实支付）；仅供展示，不参与业务判断 */
  pay_provider: string;
}

export interface PointsOrder {
  order_id: string;
  pack_id: string;
  pack_name: string;
  points: number;
  amount_fen: number;
  /**
   * `pending` 待支付（可去支付）/ `paid` 已入账 /
   * `expired` 支付码有效期已过 —— **不能再点「去支付」**，要重新下单。
   * ⚠️ 后端判过期用的阈值与支付码有效期同值（`PAY_CHARGE_VALID_SECONDS`）。
   */
  status: "pending" | "paid" | "expired";
  provider: string;
  out_trade_no: string;
  created_at: string;
  paid_at: string | null;
  /** 通道出码结果（未出过码时后端不返回这些字段） */
  pay_type?: PayType;
  pay_info?: string;
  trade_no?: string;
  pay_created_at?: string;
}

/**
 * 出码形态：四选一，决定前端怎么把 `pay_info` 呈现给用户。
 * `image` 是微信个人收款通道特有的：平台只配了**收款码图片**（没配内容）时，
 * `pay_info` 是图片地址，必须按 `<img>` 显示 —— 拿它去生成二维码会变成一个
 * 「指向图片」的二维码，扫了没有用。
 */
export type PayType = "" | "qrcode" | "jump" | "urlscheme" | "image";

export interface PayCharge {
  pay_type: PayType;
  /** 渲染所需内容：qrcode=二维码内容 / jump=跳转链接 / urlscheme=小程序链接 / image=图片地址 */
  pay_info: string;
  trade_no: string;
  /** true = 落在复用窗口内，沿用了上一次的出码结果（未重复向下游下单） */
  reused: boolean;
  /** 支付码有效期（秒），**由后端下发** —— 界面按它显示「请在 N 分钟内完成支付」，
   *  不硬编码。它必须与支付平台后台那条通道的「识别有效期」同值，
   *  否则界面承诺的时长远大于平台实际认单的时长 ⇒ 超时付款必然漏单。 */
  valid_seconds?: number;
}

/**
 * 后端业务错误码：同一面额已经有一笔待付单占着（镜像 `service.SAME_AMOUNT_BUSY_*_CODE`）。
 * 前端据此弹「只有确认按钮」的告知弹窗 —— 而不是去匹配那句中文文案。
 *
 * 为什么是两个码而不是一个：弹窗里还有一句"那你现在能做什么"，而它**只对"占位的是
 * 自己"成立**（自己占着 → 手里确实有一笔能付的单，可以去「最近订单」付掉；别人占着
 * → 用户根本没下过单，再指引他去找自己那笔待付单只会让人懵）。正文文案两个码相同，
 * 差别只在前端补的那句指引，见 `lib/payAlert.ts`。
 */
export const ERR_SAME_AMOUNT_BUSY_SELF = "same_amount_busy_self";
export const ERR_SAME_AMOUNT_BUSY_OTHER = "same_amount_busy_other";

/** 「同面额被占」的两个子类，决定弹窗里那句指引给不给。 */
export type SameAmountBusyKind = "self" | "other";

/**
 * 判断这个错误是不是「同一面额被占用」，并区分**占位的是自己还是别人**。
 * 不是这一类就返回 null（调用方据此走 toast 而不是弹窗）。
 */
export function sameAmountBusyKind(e: unknown): SameAmountBusyKind | null {
  if (!(e instanceof ApiError)) return null;
  if (e.code === ERR_SAME_AMOUNT_BUSY_SELF) return "self";
  if (e.code === ERR_SAME_AMOUNT_BUSY_OTHER) return "other";
  return null;
}

/**
 * 带状态码与业务错误码的接口错误。
 *
 * 为什么需要它：同一个 HTTP 409 底下可能是好几种业务原因（订单已过期 / 已支付 /
 * 同面额被占），前端要分支就必须拿到**机器可读**的 `code`；也不该退而匹配中文文案
 * —— 文案会改，而匹配文案的代码不会跟着改。
 * 它 `extends Error`，所以现存的 `e instanceof Error ? e.message : …` 全部照旧可用。
 */
export class ApiError extends Error {
  /** HTTP 状态码 */
  readonly status: number;
  /** 业务错误码（后端 `MemberError.err_code`）；后端没给就是空串 */
  readonly code: string;

  constructor(message: string, status: number, code = "") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

async function fetchJSON<T>(url: string, options?: RequestInit): Promise<T> {
  const resp = await authFetch(url, options);
  if (!resp.ok) {
    let detail = resp.statusText;
    let code = "";
    try {
      const body = await resp.json();
      detail = body.detail || body.error || detail;
      code = body.code || "";
    } catch {
      detail = await resp.text().catch(() => detail);
    }
    throw new ApiError(detail, resp.status, code);
  }
  return resp.json();
}

const jsonInit = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const memberApi = {
  async requestEmailCode(email: string) {
    return fetchJSON<{ ok: boolean; ttl_minutes: number }>("/api/auth/email-code",
      jsonInit("POST", { email }));
  },

  async registerEmail(email: string, code: string, password: string, nickname: string) {
    return fetchJSON<{ token: string; user: MemberUser }>("/api/auth/register-email",
      jsonInit("POST", { email, code, password, nickname }));
  },

  async login(username: string, password: string) {
    return fetchJSON<{ token: string; user: MemberUser }>("/api/auth/login",
      jsonInit("POST", { username, password }));
  },

  async logout() {
    return fetchJSON<{ ok: boolean }>("/api/auth/logout", { method: "POST" });
  },

  async updateProfile(nickname?: string, bio?: string) {
    return fetchJSON<{ user: MemberUser }>("/api/users/me",
      jsonInit("PATCH", { nickname, bio }));
  },

  async changePassword(oldPassword: string, newPassword: string) {
    return fetchJSON<{ ok: boolean; message: string }>("/api/users/me/password",
      jsonInit("POST", { old_password: oldPassword, new_password: newPassword }));
  },

  async balance() {
    return fetchJSON<{ points: number }>("/api/points/balance");
  },

  async logs(offset = 0, limit = 20) {
    return fetchJSON<{ total: number; offset: number; logs: PointLog[] }>(
      `/api/points/logs?offset=${offset}&limit=${limit}`);
  },

  async redeem(code: string) {
    return fetchJSON<{ added: number; balance: number; code: string }>("/api/points/redeem",
      jsonInit("POST", { code }));
  },

  async checkinStatus() {
    return fetchJSON<{ checked_in_today: boolean; total_days: number; bonus: number }>(
      "/api/points/checkin");
  },

  async checkin() {
    return fetchJSON<{ added: number; balance: number; date: string }>("/api/points/checkin",
      { method: "POST" });
  },

  // ─── 积分商城 ─────────────────────────────────────────────

  async packs() {
    return fetchJSON<PointsPackList>("/api/points/packs");
  },

  async orders(offset = 0, limit = 20) {
    return fetchJSON<{ total: number; offset: number; orders: PointsOrder[] }>(
      `/api/points/orders?offset=${offset}&limit=${limit}`);
  },

  /**
   * 单条订单（**支付后轮询**用）。
   * 只读：后端不会因为被轮询而把订单改成已支付 —— 入账只发生在支付回调里。
   */
  async order(orderId: string) {
    return fetchJSON<{ order: PointsOrder }>(`/api/points/orders/${orderId}`);
  },

  /** 下单只落一条 pending 订单，**不发积分**（积分在支付成功回调里发）。 */
  async createOrder(packId: string) {
    return fetchJSON<{ order: PointsOrder }>("/api/points/orders",
      jsonInit("POST", { pack_id: packId }));
  },

  /**
   * 向当前支付通道下单（出码）。前端按返回的 `pay_type` 渲染：
   * qrcode → 二维码；jump → 跳转链接；urlscheme → 唤起小程序。
   * 同一订单在复用窗口内重复调用返回 `reused: true`（沿用上次结果）。
   */
  async payOrder(orderId: string) {
    return fetchJSON<{ order: PointsOrder; pay: PayCharge }>(
      `/api/points/orders/${orderId}/pay`, { method: "POST" });
  },

  /** 模拟支付成功（演示用）。与真实回调同一条入账路径，同样幂等。 */
  async mockPay(orderId: string) {
    return fetchJSON<{ order: PointsOrder; added: number; balance: number; already_paid: boolean }>(
      `/api/points/orders/${orderId}/mock-pay`, { method: "POST" });
  },
};
