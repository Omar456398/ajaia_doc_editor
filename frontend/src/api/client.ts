/**
 * URL-agnostic API & WebSocket client.
 * Defaults to relative `/api` on the current origin, or uses `VITE_API_BASE_URL`
 * if frontend and backend are deployed on separate domains without a proxy rewrite.
 */
const rawEnvBase = (
  (import.meta as unknown as { env?: Record<string, string | undefined> }).env
    ?.VITE_API_BASE_URL ?? ""
)
  .trim()
  .replace(/\/+$/, "");

export const API_BASE_PATH = rawEnvBase
  ? rawEnvBase.endsWith("/api")
    ? rawEnvBase
    : `${rawEnvBase}/api`
  : "/api";

export function resolveAttachmentUrl(url: string): string {
  if (!url) return url;
  if (url.startsWith("/api/") && API_BASE_PATH !== "/api") {
    return `${API_BASE_PATH}${url.slice(4)}`;
  }
  return url;
}

export interface User {
  id: number;
  username: string;
  full_name: string;
  avatar_color: string;
}

export interface Collaborator {
  user_id: number;
  username: string;
  full_name: string;
  avatar_color: string;
  is_owner: boolean;
  cursor_pos: number;
  base_cursor_pos?: number;
  is_editing: boolean;
  is_selection_perimeter?: boolean;
  editing_disabled_by_selection?: boolean;
  has_pending_update?: boolean;
  base_perimeter_start?: number;
  base_perimeter_end?: number;
  perimeter_start: number;
  perimeter_end: number;
  safe_zone_start: number;
  safe_zone_end: number;
  expires_in_seconds: number;
}

export interface DocSummary {
  id: number;
  title: string;
  preview: string;
  is_owner: boolean;
  owner_name: string;
  collaborators: Collaborator[];
  pending_logs_count: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface EditLogEntry {
  id: number;
  user_id: number;
  username: string;
  pos_start: number;
  pos_end: number;
  inserted_text: string;
  delta: number;
  created_at: string | null;
}

export interface PerimeterConfig {
  width: number;
  half_width: number;
  border_hysteresis: number;
  expiry_seconds: number;
  flush_inactivity_seconds: number;
}

export interface AttachmentItem {
  id: number;
  doc_id: number | null;
  user_id: number | null;
  filename: string;
  content_type: string;
  size_bytes: number;
  url: string;
  created_at: string | null;
}

export interface DocDetail {
  id: number;
  title: string;
  base_content: string;
  content: string;
  is_owner: boolean;
  pending_logs_count: number;
  recent_logs: EditLogEntry[];
  collaborators: Collaborator[];
  perimeter_config: PerimeterConfig;
  updated_at: string | null;
}

export async function apiFetch<T>(
  endpoint: string,
  options?: RequestInit,
  token?: string | null
): Promise<T> {
  const normalizedEndpoint = endpoint.startsWith("/")
    ? endpoint
    : `/${endpoint}`;
  const url = `${API_BASE_PATH}${normalizedEndpoint}`;

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options?.headers as Record<string, string> | undefined),
  };
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  const response = await fetch(url, {
    ...options,
    headers,
  });

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // ignore json parse error
    }
    throw new Error(detail || `API error (${response.status})`);
  }

  return response.json() as Promise<T>;
}

export function getDocWebSocketUrl(docId: number, token: string): string {
  if (API_BASE_PATH.startsWith("http://") || API_BASE_PATH.startsWith("https://")) {
    const wsBase = API_BASE_PATH.replace(/^http/, "ws");
    return `${wsBase}/ws/docs/${docId}?token=${encodeURIComponent(token)}`;
  }
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const host = window.location.host;
  return `${proto}//${host}${API_BASE_PATH}/ws/docs/${docId}?token=${encodeURIComponent(token)}`;
}

export function fetchDummyUsers(): Promise<{
  users: User[];
  default_password: string;
}> {
  return apiFetch("/users/dummy");
}

export function loginUser(
  username: string,
  password: string
): Promise<{ token: string; user: User }> {
  return apiFetch("/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

export function fetchMe(token: string): Promise<{ user: User }> {
  return apiFetch("/auth/me", undefined, token);
}

export function fetchDocs(
  token: string,
  searchQuery?: string
): Promise<{ docs: DocSummary[] }> {
  const q = searchQuery?.trim();
  const path = q ? `/docs?q=${encodeURIComponent(q)}` : "/docs";
  return apiFetch(path, undefined, token);
}

export function createDoc(
  token: string,
  title: string,
  content?: string
): Promise<{ id: number; title: string }> {
  return apiFetch(
    "/docs",
    {
      method: "POST",
      body: JSON.stringify({ title, content }),
    },
    token
  );
}

export async function uploadDocumentFile(
  token: string,
  file: File
): Promise<{ id: number; title: string; content: string }> {
  const formData = new FormData();
  formData.append("file", file, file.name);
  const response = await fetch(`${API_BASE_PATH}/docs/upload`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
    },
    body: formData,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // ignore
    }
    throw new Error(detail || `Upload failed (${response.status})`);
  }
  return response.json() as Promise<{
    id: number;
    title: string;
    content: string;
  }>;
}

export async function uploadDocAttachment(
  token: string,
  docId: number,
  file: File
): Promise<AttachmentItem> {
  const formData = new FormData();
  formData.append("file", file, file.name || "image.png");
  const response = await fetch(`${API_BASE_PATH}/docs/${docId}/attachments`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
    },
    body: formData,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // ignore
    }
    throw new Error(detail || `Attachment upload failed (${response.status})`);
  }
  return response.json() as Promise<AttachmentItem>;
}

export function fetchDocAttachments(
  token: string,
  docId: number
): Promise<{ attachments: AttachmentItem[] }> {
  return apiFetch(`/docs/${docId}/attachments`, undefined, token);
}

export function fetchDocDetail(
  token: string,
  docId: number
): Promise<DocDetail> {
  return apiFetch(`/docs/${docId}`, undefined, token);
}

export function updateDocTitle(
  token: string,
  docId: number,
  title: string
): Promise<{ id: number; title: string }> {
  return apiFetch(
    `/docs/${docId}`,
    {
      method: "PATCH",
      body: JSON.stringify({ title }),
    },
    token
  );
}

export function deleteDoc(
  token: string,
  docId: number
): Promise<{ status: string; id: number }> {
  return apiFetch(`/docs/${docId}`, { method: "DELETE" }, token);
}

export function addContributor(
  token: string,
  docId: number,
  userId: number
): Promise<{ status: string; collaborators: Collaborator[] }> {
  return apiFetch(
    `/docs/${docId}/contributors`,
    {
      method: "POST",
      body: JSON.stringify({ user_id: userId }),
    },
    token
  );
}

export function removeContributor(
  token: string,
  docId: number,
  userId: number
): Promise<{ status: string; collaborators: Collaborator[] }> {
  return apiFetch(
    `/docs/${docId}/contributors/${userId}`,
    { method: "DELETE" },
    token
  );
}

export function flushDocLogs(
  token: string,
  docId: number
): Promise<{
  status: string;
  flushed: boolean;
  content: string;
  pending_logs_count: number;
  collaborators: Collaborator[];
}> {
  return apiFetch(`/docs/${docId}/flush`, { method: "POST" }, token);
}

export function submitPerimeterUpdateHttp(
  token: string,
  docId: number,
  payload: {
    pos_start?: number;
    pos_end?: number;
    replacement_text: string;
    full_content?: string;
    cursor_pos: number;
    selection_collapsed: boolean;
    flush_immediately?: boolean;
  },
  keepalive = false
): Promise<{
  status: string;
  content: string;
  base_content?: string;
  pending_logs_count: number;
  flushed_on_perimeter_shift?: boolean;
  collaborators: Collaborator[];
  message?: string;
}> {
  return apiFetch(
    `/docs/${docId}/edits`,
    {
      method: "POST",
      body: JSON.stringify(payload),
      keepalive,
    },
    token
  );
}

export function updateCursorOrSelectionHttp(
  token: string,
  docId: number,
  payload: {
    cursor_pos: number;
    selection_start: number | null;
    selection_end: number | null;
    is_editing: boolean;
  }
): Promise<{
  status: string;
  flushed?: boolean;
  editing_disabled?: boolean;
  message?: string;
  collaborators: Collaborator[];
}> {
  return apiFetch(
    `/docs/${docId}/cursor`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
    token
  );
}
