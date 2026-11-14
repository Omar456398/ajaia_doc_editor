import { useEffect, useState } from "react";
import { API_BASE_PATH, fetchHealth, type HealthResponse } from "./api/client";

export function App() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(true);

  const checkBackend = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchHealth();
      setHealth(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to reach /api/health");
      setHealth(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void checkBackend();
  }, []);

  const currentOrigin =
    typeof window !== "undefined" ? window.location.origin : "http://xxx.xxx.xxx";

  return (
    <main className="min-h-screen flex items-center justify-center p-6">
      <div className="max-w-xl w-full rounded-2xl border border-slate-800 bg-slate-900/80 p-8 shadow-xl space-y-6">
        <div className="space-y-2">
          <span className="inline-flex items-center rounded-full bg-indigo-500/10 px-3 py-1 text-xs font-medium text-indigo-400 ring-1 ring-inset ring-indigo-500/20">
            Full-Stack Starter
          </span>
          <h1 className="text-2xl font-semibold tracking-tight text-white">
            Ajaia Doc Editor
          </h1>
          <p className="text-sm text-slate-400">
            React + FastAPI + PostgreSQL with URL-agnostic unified routing.
          </p>
        </div>

        <div className="rounded-xl border border-slate-800 bg-slate-950 p-4 space-y-2 text-xs font-mono">
          <div className="flex justify-between text-slate-400">
            <span>Frontend Route:</span>
            <span className="text-slate-200">{currentOrigin}/</span>
          </div>
          <div className="flex justify-between text-slate-400">
            <span>Backend API Route:</span>
            <span className="text-indigo-400">
              {currentOrigin}
              {API_BASE_PATH}
            </span>
          </div>
        </div>

        <div className="rounded-xl border border-slate-800 bg-slate-950/60 p-4 space-y-3">
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium text-slate-300">
              System Health (<code className="text-indigo-400">/api/health</code>)
            </span>
            <button
              type="button"
              onClick={() => void checkBackend()}
              disabled={loading}
              className="rounded-lg bg-slate-800 px-3 py-1.5 text-xs font-medium text-slate-200 hover:bg-slate-700 disabled:opacity-50 transition cursor-pointer"
            >
              {loading ? "Checking..." : "Refresh"}
            </button>
          </div>

          {error && (
            <div className="rounded-lg bg-rose-500/10 border border-rose-500/20 p-3 text-xs text-rose-300">
              {error}
            </div>
          )}

          {health && (
            <div className="grid grid-cols-2 gap-3 text-xs">
              <div className="rounded-lg bg-slate-900 p-3 border border-slate-800">
                <div className="text-slate-400">FastAPI Status</div>
                <div className="mt-1 font-semibold text-emerald-400 uppercase">
                  {health.status}
                </div>
              </div>
              <div className="rounded-lg bg-slate-900 p-3 border border-slate-800">
                <div className="text-slate-400">PostgreSQL Status</div>
                <div
                  className={`mt-1 font-semibold uppercase ${
                    health.database.status === "connected"
                      ? "text-emerald-400"
                      : "text-amber-400"
                  }`}
                >
                  {health.database.status}
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </main>
  );
}

export default App;
