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
  pay_channel_ready: boolean;
}

export interface PointsOrder {
  order_id: string;
  pack_id: string;
  pack_name: string;
  points: number;
  amount_fen: number;
  status: "pending" | "paid";
  provider: string;
  out_trade_no: string;
  created_at: string;
  paid_at: string | null;
}

async function fetchJSON<T>(url: string, options?: RequestInit): Promise<T> {
  const resp = await authFetch(url, options);
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = body.detail || body.error || detail;
    } catch {
      detail = await resp.text().catch(() => detail);
    }
    throw new Error(detail);
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

  /** 下单只落一条 pending 订单，**不发积分**（积分在支付成功回调里发）。 */
  async createOrder(packId: string) {
    return fetchJSON<{ order: PointsOrder }>("/api/points/orders",
      jsonInit("POST", { pack_id: packId }));
  },

  /** 模拟支付成功（演示用）。与真实回调同一条入账路径，同样幂等。 */
  async mockPay(orderId: string) {
    return fetchJSON<{ order: PointsOrder; added: number; balance: number; already_paid: boolean }>(
      `/api/points/orders/${orderId}/mock-pay`, { method: "POST" });
  },
};
