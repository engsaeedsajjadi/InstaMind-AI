"use client";

/**
 * API client for the InstaMind backend.
 *
 * Design:
 * - All requests are relative (`/api/v1/...`) and proxied server-side by
 *   Next rewrites — the browser never sees a backend address.
 * - Bearer token + active workspace id are attached automatically.
 * - On 401 the rotating refresh token is used once; if it fails, the user
 *   is signed out locally. A reissue inside the backend's rotation grace
 *   window returns an EMPTY refresh_token — we must not overwrite the
 *   stored replacement with it.
 */

import type {
  ConnectStartResponse,
  ContentRead,
  ProblemDetail,
  PublishingJobRead,
  SocialAccountRead,
  TokenResponse,
  UserRead,
  WorkspaceRead,
} from "./types";

const BASE = "/api/v1";
const AUTH_KEY = "instamind.authenticated";
const WS_KEY = "instamind.workspace_id";

export class ApiError extends Error {
  code: string;
  status: number;
  details?: ProblemDetail["errors"];

  constructor(problem: ProblemDetail, status: number) {
    super(problem.detail || problem.title || "خطایی رخ داد.");
    this.code = problem.code || "unknown_error";
    this.status = status;
    this.details = problem.errors;
  }
}

export interface StoredAuth {
  authenticated: true;
}

export function getAuth(): StoredAuth | null {
  if (typeof window === "undefined") return null;
  return window.sessionStorage.getItem(AUTH_KEY) === "1" ? { authenticated: true } : null;
}

function setAuth(auth: StoredAuth | null): void {
  if (auth === null) window.sessionStorage.removeItem(AUTH_KEY);
  else window.sessionStorage.setItem(AUTH_KEY, "1");
}

export function isAuthenticated(): boolean {
  return getAuth() !== null;
}

export function getWorkspaceId(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(WS_KEY);
}

export function setWorkspaceId(id: string | null): void {
  if (id === null) window.localStorage.removeItem(WS_KEY);
  else window.localStorage.setItem(WS_KEY, id);
}

export function signOut(): void {
  setAuth(null);
  setWorkspaceId(null);
}

async function parseProblem(response: Response): Promise<ApiError> {
  try {
    const problem = (await response.json()) as ProblemDetail;
    return new ApiError(problem, response.status);
  } catch {
    return new ApiError({ detail: `خطای غیرمنتظره (HTTP ${response.status})` }, response.status);
  }
}

/** Try once to rotate the refresh token. Returns false when the session is dead. */
async function tryRefresh(): Promise<boolean> {
  if (!getAuth()) return false;
  const response = await fetch(`${BASE}/auth/browser/refresh`, {
    method: "POST",
    credentials: "include",
    cache: "no-store",
  });
  if (!response.ok) {
    signOut();
    return false;
  }
  const tokens = (await response.json()) as TokenResponse;
  // Inside the rotation grace window the backend reissues ONLY an access
  // token (empty string) — keep the replacement refresh token we already hold.
  setAuth({ authenticated: true });
  return true;
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  options: { retry?: boolean } = {},
): Promise<T> {
  const { retry = true } = options;
  const headers = new Headers(init.headers);
  if (!headers.has("Content-Type") && init.body) {
    headers.set("Content-Type", "application/json");
  }
  // Browser auth is carried by HttpOnly cookies; never put JWTs in JS-accessible storage.
  const workspaceId = getWorkspaceId();
  if (workspaceId) headers.set("X-Workspace-Id", workspaceId);

  const response = await fetch(`${BASE}${path}`, { ...init, headers, credentials: "include", cache: "no-store" });
  if (response.status === 401 && retry && (await tryRefresh())) {
    return request<T>(path, init, { retry: false });
  }
  if (!response.ok) {
    // A second 401 means the session is really over.
    if (response.status === 401) signOut();
    throw await parseProblem(response);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

// --------------------------------------------------------------------------- //
// auth
// --------------------------------------------------------------------------- //

export async function login(email: string, password: string): Promise<void> {
  await request<UserRead>("/auth/browser/login", {
    method: "POST",
    body: JSON.stringify({ email, password, device_label: "web" }),
  });
  setAuth({ authenticated: true });
}

export async function register(email: string, password: string, fullName: string): Promise<void> {
  await request<UserRead>("/auth/register", {
    method: "POST",
    body: JSON.stringify({ email, password, full_name: fullName }),
  });
  await login(email, password);
}

export async function logout(): Promise<void> {
  try {
    await request<void>("/auth/browser/logout", { method: "POST" });
  } finally {
    signOut();
  }
}

export async function fetchMe(): Promise<UserRead> {
  return request<UserRead>("/auth/me");
}

// --------------------------------------------------------------------------- //
// workspaces
// --------------------------------------------------------------------------- //

export async function fetchWorkspaces(): Promise<WorkspaceRead[]> {
  return request<WorkspaceRead[]>("/workspaces");
}

export async function createWorkspace(name: string): Promise<WorkspaceRead> {
  return request<WorkspaceRead>("/workspaces", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
}

// --------------------------------------------------------------------------- //
// instagram
// --------------------------------------------------------------------------- //

export async function fetchAccounts(): Promise<SocialAccountRead[]> {
  return request<SocialAccountRead[]>("/social-accounts");
}

export async function startConnect(apiPath: "INSTAGRAM_LOGIN" | "FACEBOOK_LOGIN"): Promise<ConnectStartResponse> {
  return request<ConnectStartResponse>("/social-accounts/connect", {
    method: "POST",
    body: JSON.stringify({ api_path: apiPath }),
  });
}

export async function disconnectAccount(accountId: string, purge: boolean): Promise<SocialAccountRead> {
  return request<SocialAccountRead>(`/social-accounts/${accountId}`, {
    method: "DELETE",
    body: JSON.stringify({ purge_platform_data: purge }),
  });
}

export async function refreshAccountToken(accountId: string): Promise<SocialAccountRead> {
  return request<SocialAccountRead>(`/social-accounts/${accountId}/refresh-token`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

// --------------------------------------------------------------------------- //
// content / publishing (dashboard stats)
// --------------------------------------------------------------------------- //

export async function fetchContents(): Promise<ContentRead[]> {
  return request<ContentRead[]>("/contents?limit=200");
}

export async function fetchPublishingJobs(): Promise<PublishingJobRead[]> {
  return request<PublishingJobRead[]>("/publishing/jobs?limit=200");
}


export interface ConversationRow {
  id: string; participant_username?: string | null; participant_name?: string | null;
  status: string; is_read: boolean; last_message_at?: string | null;
  needs_human: boolean; customer_id?: string | null;
}
export interface MessageRow {
  id: string; conversation_id: string; direction: string; text: string;
  sender_kind: string; sent_at?: string | null; status: string;
}
export interface CommentRow {
  id: string; external_comment_id: string; media_id?: string | null;
  from_username?: string | null; text: string; status: string; is_hidden: boolean;
  needs_human: boolean; posted_at?: string | null;
}
export interface CustomerRow {
  id: string; username?: string | null; display_name?: string | null;
  email?: string | null; lead_score: number; stage: string; tags: string[];
}
export interface AnalyticsRow {
  id: string; metric: string; period: string; since: string; until: string;
  value?: number | null; is_available: boolean;
}

export async function fetchConversations(): Promise<ConversationRow[]> {
  return request<ConversationRow[]>("/inbox/conversations?limit=200");
}
export async function fetchMessages(conversationId: string): Promise<MessageRow[]> {
  return request<MessageRow[]>(`/inbox/conversations/${conversationId}/messages?limit=500`);
}
export async function sendMessage(conversationId: string, text: string, humanAgent = false): Promise<MessageRow> {
  return request<MessageRow>(`/inbox/conversations/${conversationId}/send`, {
    method: "POST", body: JSON.stringify({ text, human_agent: humanAgent }),
  });
}
export async function fetchComments(): Promise<CommentRow[]> {
  return request<CommentRow[]>("/comments?limit=200");
}
export async function replyComment(commentId: string, text: string, privateReply = false): Promise<CommentRow> {
  return request<CommentRow>(`/comments/${commentId}/reply`, {
    method: "POST", body: JSON.stringify({ text, private: privateReply }),
  });
}
export async function hideComment(commentId: string): Promise<CommentRow> {
  return request<CommentRow>(`/comments/${commentId}/hide`, { method: "POST" });
}
export async function fetchCustomers(): Promise<CustomerRow[]> {
  return request<CustomerRow[]>("/crm/customers?limit=200");
}
export async function updateCustomer(customerId: string, patch: Record<string, unknown>): Promise<CustomerRow> {
  return request<CustomerRow>(`/crm/customers/${customerId}`, {
    method: "PATCH", body: JSON.stringify(patch),
  });
}
export async function fetchAnalytics(): Promise<AnalyticsRow[]> {
  return request<AnalyticsRow[]>("/analytics/snapshots?limit=500");
}
