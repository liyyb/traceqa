// 本机令牌与接口封装（手册 §8：仅回环地址 + 本机访问令牌）
let TOKEN = "";

export function initToken(): string {
  if (!TOKEN) {
    const p = new URLSearchParams(window.location.search);
    TOKEN = p.get("token") || sessionStorage.getItem("tq_token") || "";
    if (TOKEN) sessionStorage.setItem("tq_token", TOKEN);
  }
  return TOKEN;
}

export function tokenParam(): string {
  return encodeURIComponent(initToken());
}

export class ApiError extends Error {
  status: number;
  body: any;
  constructor(status: number, body: any) {
    super(typeof body?.error === "string" ? body.error : `HTTP ${status}`);
    this.status = status;
    this.body = body;
  }
}

export async function api<T = any>(path: string, opts: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    "X-TraceQA-Token": initToken(),
    ...(opts.body && !(opts.body instanceof FormData)
      ? { "Content-Type": "application/json" }
      : {}),
    ...(opts.headers as Record<string, string> || {}),
  };
  const res = await fetch(`/api${path}`, { ...opts, headers });
  if (!res.ok) {
    let body: any = null;
    try { body = await res.json(); } catch { /* ignore */ }
    throw new ApiError(res.status, body);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export function newRequestId(prefix = "req"): string {
  const id = sessionStorage.getItem(`${prefix}_pending`);
  return id || `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

export function requestIdFor(scope: string, fresh = false): string {
  const key = `tq_req_${scope}`;
  if (!fresh) {
    const old = sessionStorage.getItem(key);
    if (old) return old;
  }
  const id = `${scope}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  sessionStorage.setItem(key, id);
  return id;
}

export function clearRequestId(scope: string) {
  sessionStorage.removeItem(`tq_req_${scope}`);
}
