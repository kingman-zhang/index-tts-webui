/** 会员/积分 API 客户端。 */
import { authFetch } from "@/lib/auth";
import type { MemberUser } from "@/lib/auth";

export interface PointLog {
  id: string;
  user_id: string;
  delta: number;
  balance_after: number;
  kind: "earn" | "spend" | "redeem" | "checkin" | "grant" | "refund";
  reason: string;
  ref: string;
  created_at: string;
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
};
