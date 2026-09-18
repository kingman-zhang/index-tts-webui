/** 会员模块前端全局状态：token 持久化 + 用户信息（零依赖，useSyncExternalStore 订阅）。 */
import { useSyncExternalStore } from "react";

export interface MemberUser {
  user_id: string;
  username: string;
  nickname: string;
  bio: string;
  points: number;
  disabled: boolean;
  created_at?: string;
  last_login_at?: string;
}

const TOKEN_KEY = "wb-auth-token";

interface AuthState {
  ready: boolean;          // 是否完成首次恢复
  token: string | null;
  user: MemberUser | null;
}

let state: AuthState = { ready: false, token: localStorage.getItem(TOKEN_KEY), user: null };
const listeners = new Set<() => void>();

function emit() {
  state = { ...state };
  listeners.forEach(l => l());
}

function setAuth(patch: Partial<AuthState>) {
  Object.assign(state, patch);
  emit();
}

/** 带 token 的 fetch；401 时自动清除本地登录态。 */
export async function authFetch(url: string, options: RequestInit = {}): Promise<Response> {
  const headers = new Headers(options.headers);
  if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
  const resp = await fetch(url, { ...options, headers });
  if (resp.status === 401 && state.token) {
    localStorage.removeItem(TOKEN_KEY);
    setAuth({ token: null, user: null });
  }
  return resp;
}

/** 恢复登录态（页面加载时调用一次；token 过期则静默清除）。 */
export async function initAuth(): Promise<void> {
  if (!state.token) {
    setAuth({ ready: true });
    return;
  }
  try {
    const resp = await authFetch("/api/auth/me");
    if (resp.ok) {
      const body = await resp.json();
      setAuth({ ready: true, user: body.user });
    } else {
      localStorage.removeItem(TOKEN_KEY);
      setAuth({ ready: true, token: null, user: null });
    }
  } catch {
    setAuth({ ready: true });
  }
}

export function setSession(token: string, user: MemberUser) {
  localStorage.setItem(TOKEN_KEY, token);
  setAuth({ token, user });
}

export function updateUser(user: MemberUser) {
  setAuth({ user });
}

export function clearSession() {
  localStorage.removeItem(TOKEN_KEY);
  setAuth({ token: null, user: null });
}

/** 刷新积分/资料（签到、兑换、扣费后调用）。 */
export async function refreshUser(): Promise<MemberUser | null> {
  if (!state.token) return null;
  try {
    const resp = await authFetch("/api/auth/me");
    if (resp.ok) {
      const body = await resp.json();
      setAuth({ user: body.user });
      return body.user;
    }
  } catch { /* 忽略 */ }
  return null;
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

function getSnapshot(): AuthState {
  return state;
}

export function useAuth(): AuthState {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

/** 零依赖路由跳转（与 App.tsx 的 popstate 路由配套）。 */
export function navigate(path: string) {
  window.history.pushState(null, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}
