/** API contracts mirroring apps/backend/app/api/schemas.py. */

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: "Bearer";
  expires_in: number;
  session_id: string | null;
}

export interface UserRead {
  id: string;
  email: string;
  full_name: string;
  locale: string;
  timezone: string;
  is_active: boolean;
  is_superuser: boolean;
  email_verified_at: string | null;
}

export interface WorkspaceRead {
  id: string;
  name: string;
  slug: string;
  owner_id: string;
  default_locale: string;
  default_timezone: string;
  calendar_system: string;
}

export interface SocialAccountRead {
  id: string;
  workspace_id: string;
  platform: string;
  api_path: "INSTAGRAM_LOGIN" | "FACEBOOK_LOGIN";
  external_account_id: string;
  username: string;
  display_name: string | null;
  account_type: string | null; // BUSINESS | CREATOR
  media_count: number | null;
  followers_count: number | null;
  capabilities: Record<string, boolean>;
  granted_scopes: string[];
  status: "CONNECTED" | "TOKEN_EXPIRED" | "DISCONNECTED" | "ERROR" | "REVOKED";
  status_reason: string | null;
  connected_at: string;
}

export interface ConnectStartResponse {
  authorize_url: string;
  state: string;
  api_path: string;
}

export interface ContentRead {
  id: string;
  status: string;
  content_type: string;
  title: string;
}

export interface PublishingJobRead {
  id: string;
  state: string;
}

/** RFC 9457 problem body the backend returns on every error. */
export interface ProblemDetail {
  type?: string;
  title?: string;
  detail?: string;
  code?: string;
  status?: number;
  errors?: { field: string; code: string; message: string }[];
}
