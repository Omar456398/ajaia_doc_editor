/**
 * URL-agnostic API client.
 * Always uses the relative `/api` path on the current origin (`xxx.xxx.xxx/api`),
 * working seamlessly behind Vite's dev proxy, FastAPI static serving, or any reverse proxy.
 */
export const API_BASE_PATH = "/api";

export interface HealthResponse {
  status: string;
  service: string;
  environment: string;
  api_prefix: string;
  database: {
    status: "connected" | "unavailable" | "disconnected";
    error: string | null;
  };
}

export async function apiFetch<T>(
  endpoint: string,
  options?: RequestInit
): Promise<T> {
  const normalizedEndpoint = endpoint.startsWith("/")
    ? endpoint
    : `/${endpoint}`;
  const url = `${API_BASE_PATH}${normalizedEndpoint}`;

  const response = await fetch(url, {
    headers: {
      "Content-Type": "application/json",
      ...options?.headers,
    },
    ...options,
  });

  if (!response.ok) {
    throw new Error(`API error (${response.status}): ${response.statusText}`);
  }

  return response.json() as Promise<T>;
}

export function fetchHealth(): Promise<HealthResponse> {
  return apiFetch<HealthResponse>("/health");
}
