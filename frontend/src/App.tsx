import { useCallback, useEffect, useState } from "react";
import {
  createDoc,
  deleteDoc,
  fetchDocs,
  fetchDummyUsers,
  fetchMe,
  loginUser,
  type DocSummary,
  type User,
} from "./api/client";
import { DocEditor } from "./components/DocEditor";
import { markdownToPlainText } from "./utils/markdown";

const SESSION_TOKEN_KEY = "ajaia_session_token";
const SESSION_ACTIVE_DOC_KEY = "ajaia_active_doc_id";

export function App() {
  const [token, setToken] = useState<string | null>(() =>
    sessionStorage.getItem(SESSION_TOKEN_KEY)
  );
  const [currentUser, setCurrentUser] = useState<User | null>(null);
  const [dummyUsers, setDummyUsers] = useState<User[]>([]);
  const [defaultPassword, setDefaultPassword] = useState("password123");

  // Login form state
  const [username, setUsername] = useState("alex.rivera");
  const [password, setPassword] = useState("password123");
  const [authError, setAuthError] = useState<string | null>(null);
  const [authLoading, setAuthLoading] = useState(false);

  // Dashboard state
  const [docs, setDocs] = useState<DocSummary[]>([]);
  const [docsLoading, setDocsLoading] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [activeDocId, setActiveDocIdState] = useState<number | null>(() => {
    const saved = sessionStorage.getItem(SESSION_ACTIVE_DOC_KEY);
    if (saved && /^\d+$/.test(saved)) {
      return Number(saved);
    }
    return null;
  });

  const setActiveDocId = useCallback((id: number | null) => {
    setActiveDocIdState(id);
    if (id === null) {
      sessionStorage.removeItem(SESSION_ACTIVE_DOC_KEY);
    } else {
      sessionStorage.setItem(SESSION_ACTIVE_DOC_KEY, String(id));
    }
  }, []);

  // Load 20 dummy users
  useEffect(() => {
    void fetchDummyUsers()
      .then((res) => {
        setDummyUsers(res.users);
        if (res.default_password) {
          setDefaultPassword(res.default_password);
        }
      })
      .catch(() => {
        // ignore if backend still starting
      });
  }, []);

  // Restore session from sessionStorage
  useEffect(() => {
    if (!token) {
      setCurrentUser(null);
      return;
    }
    void fetchMe(token)
      .then((res) => setCurrentUser(res.user))
      .catch(() => {
        sessionStorage.removeItem(SESSION_TOKEN_KEY);
        setToken(null);
        setCurrentUser(null);
      });
  }, [token]);

  const loadMyDocs = useCallback(async () => {
    if (!token) return;
    setDocsLoading(true);
    try {
      const res = await fetchDocs(token);
      setDocs(res.docs);
    } catch {
      // ignore
    } finally {
      setDocsLoading(false);
    }
  }, [token]);

  useEffect(() => {
    if (token && currentUser && activeDocId === null) {
      void loadMyDocs();
    }
  }, [token, currentUser, activeDocId, loadMyDocs]);

  const handleLogin = async (e?: React.FormEvent, quickUser?: string) => {
    if (e) e.preventDefault();
    const targetUsername = quickUser ?? username;
    const targetPassword = quickUser ? defaultPassword : password;
    setAuthLoading(true);
    setAuthError(null);
    try {
      const res = await loginUser(targetUsername, targetPassword);
      sessionStorage.setItem(SESSION_TOKEN_KEY, res.token);
      setToken(res.token);
      setCurrentUser(res.user);
      setActiveDocId(null);
    } catch (err) {
      setAuthError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setAuthLoading(false);
    }
  };

  const handleLogout = () => {
    sessionStorage.removeItem(SESSION_TOKEN_KEY);
    setToken(null);
    setCurrentUser(null);
    setActiveDocId(null);
  };

  const handleCreateDoc = async (title: string, templateContent: string) => {
    if (!token) return;
    try {
      const created = await createDoc(token, title, templateContent);
      setActiveDocId(created.id);
    } catch {
      // ignore
    }
  };

  const handleDeleteDoc = async (e: React.MouseEvent, docId: number) => {
    e.stopPropagation();
    if (!token) return;
    try {
      await deleteDoc(token, docId);
      setDocs((prev) => prev.filter((d) => d.id !== docId));
    } catch {
      // ignore
    }
  };

  // 1. Login View
  if (!token || !currentUser) {
    return (
      <div className="min-h-screen bg-[#F8FAFC] flex items-center justify-center p-6">
        <div className="max-w-5xl w-full grid grid-cols-1 lg:grid-cols-12 bg-white rounded-2xl shadow-xl border border-slate-200 overflow-hidden">
          {/* Left: Login Form */}
          <div className="lg:col-span-5 p-8 flex flex-col justify-between border-b lg:border-b-0 lg:border-r border-slate-100">
            <div className="space-y-6">
              <div className="flex items-center gap-3">
                <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-blue-600 text-white shadow-sm">
                  <svg
                    className="h-6 w-6"
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                    strokeWidth={2}
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
                    />
                  </svg>
                </div>
                <div>
                  <h1 className="text-xl font-bold text-slate-900">
                    Ajaia Docs
                  </h1>
                  <p className="text-xs text-slate-500">
                    Collaborative Document Editor
                  </p>
                </div>
              </div>

              <form onSubmit={(e) => void handleLogin(e)} className="space-y-4">
                <div>
                  <label className="block text-xs font-semibold text-slate-700 mb-1">
                    Username
                  </label>
                  <input
                    type="text"
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                    placeholder="e.g. alex.rivera"
                    className="w-full rounded-lg border border-slate-300 px-3.5 py-2 text-sm text-slate-900 focus:border-blue-600 focus:outline-hidden"
                    required
                  />
                </div>

                <div>
                  <label className="block text-xs font-semibold text-slate-700 mb-1">
                    Password
                  </label>
                  <input
                    type="password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    className="w-full rounded-lg border border-slate-300 px-3.5 py-2 text-sm text-slate-900 focus:border-blue-600 focus:outline-hidden"
                    required
                  />
                </div>

                {authError && (
                  <div className="rounded-lg bg-rose-50 border border-rose-200 px-3 py-2 text-xs text-rose-700">
                    {authError}
                  </div>
                )}

                <button
                  type="submit"
                  disabled={authLoading}
                  className="w-full rounded-lg bg-blue-600 py-2.5 text-sm font-semibold text-white hover:bg-blue-700 disabled:opacity-50 transition shadow-xs cursor-pointer"
                >
                  {authLoading ? "Signing in..." : "Sign in"}
                </button>
              </form>
            </div>

            <div className="pt-6 text-[11px] text-slate-400 space-y-1">
              <p>
                Tip: Sessions are stored per-tab (<code className="text-slate-600">sessionStorage</code>), so you can open two tabs to test multi-user 64-char perimeter locks simultaneously.
              </p>
            </div>
          </div>

          {/* Right: 20 Dummy Users One-Click Picker */}
          <div className="lg:col-span-7 bg-slate-50/70 p-8 flex flex-col">
            <div className="flex items-center justify-between mb-3">
              <div>
                <h2 className="text-sm font-semibold text-slate-900">
                  20 Seeded Dummy Collaborators
                </h2>
                <p className="text-xs text-slate-500">
                  Click any user below to sign in immediately (default password:{" "}
                  <code className="text-blue-600 font-mono">{defaultPassword}</code>)
                </p>
              </div>
              <span className="rounded-full bg-blue-100 px-2.5 py-0.5 text-xs font-medium text-blue-700">
                {dummyUsers.length} Users
              </span>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 max-h-[380px] overflow-y-auto pr-1">
              {dummyUsers.map((u, idx) => (
                <button
                  key={u.id}
                  type="button"
                  onClick={() => {
                    setUsername(u.username);
                    setPassword(defaultPassword);
                    void handleLogin(undefined, u.username);
                  }}
                  className="flex items-center gap-3 rounded-xl border border-slate-200 bg-white p-2.5 text-left hover:border-blue-400 hover:shadow-xs transition cursor-pointer"
                >
                  <div
                    className="h-8 w-8 rounded-full flex items-center justify-center text-xs font-bold text-white shrink-0"
                    style={{ backgroundColor: u.avatar_color }}
                  >
                    {u.full_name
                      .split(" ")
                      .map((n) => n[0])
                      .join("")
                      .slice(0, 2)}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="text-xs font-semibold text-slate-900 truncate flex items-center gap-1.5">
                      <span>{u.full_name}</span>
                      {idx === 0 && (
                        <span className="rounded bg-blue-50 px-1.5 py-0.2 text-[10px] text-blue-700">
                          Demo Owner
                        </span>
                      )}
                      {idx > 0 && idx <= 4 && (
                        <span className="rounded bg-emerald-50 px-1.5 py-0.2 text-[10px] text-emerald-700">
                          Invited
                        </span>
                      )}
                    </div>
                    <div className="text-[11px] text-slate-500 truncate font-mono">
                      @{u.username}
                    </div>
                  </div>
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>
    );
  }

  // 2. Document Editor View
  if (activeDocId !== null) {
    return (
      <DocEditor
        docId={activeDocId}
        currentUser={currentUser}
        allUsers={dummyUsers}
        token={token}
        onBack={() => setActiveDocId(null)}
      />
    );
  }

  // 3. Google Docs Home Dashboard View
  const filteredDocs = docs.filter((d) =>
    d.title.toLowerCase().includes(searchQuery.toLowerCase())
  );

  return (
    <div className="min-h-screen bg-[#F8FAFC] flex flex-col">
      {/* Top Navbar */}
      <header className="sticky top-0 z-20 bg-white border-b border-slate-200 px-6 py-3">
        <div className="max-w-6xl mx-auto flex items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-blue-600 text-white shadow-2xs">
              <svg
                className="h-5 w-5"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={2}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
                />
              </svg>
            </div>
            <span className="text-lg font-semibold text-slate-800">
              Ajaia <span className="text-blue-600">Docs</span>
            </span>
          </div>

          <div className="flex-1 max-w-md">
            <input
              type="search"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search your documents..."
              className="w-full rounded-full bg-[#F1F3F4] px-4 py-2 text-sm text-slate-800 placeholder-slate-500 focus:bg-white focus:ring-2 focus:ring-blue-500 focus:outline-hidden transition"
            />
          </div>

          <div className="flex items-center gap-3">
            <div className="flex items-center gap-2">
              <div
                className="h-8 w-8 rounded-full flex items-center justify-center text-xs font-bold text-white"
                style={{ backgroundColor: currentUser.avatar_color }}
              >
                {currentUser.full_name
                  .split(" ")
                  .map((n) => n[0])
                  .join("")
                  .slice(0, 2)}
              </div>
              <div className="hidden sm:block text-left">
                <div className="text-xs font-semibold text-slate-800">
                  {currentUser.full_name}
                </div>
                <div className="text-[11px] text-slate-500 font-mono">
                  @{currentUser.username}
                </div>
              </div>
            </div>
            <button
              type="button"
              onClick={handleLogout}
              className="rounded-lg border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-100 transition cursor-pointer"
            >
              Switch User
            </button>
          </div>
        </div>
      </header>

      {/* Start a new document Gallery Ribbon */}
      <section className="bg-[#F1F3F4] border-b border-slate-200 py-6 px-6">
        <div className="max-w-5xl mx-auto space-y-3">
          <h2 className="text-xs font-semibold uppercase tracking-wider text-slate-600">
            Start a new document
          </h2>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-4 max-w-2xl">
            <button
              type="button"
              onClick={() =>
                void handleCreateDoc(
                  "Untitled Document",
                  "# Untitled Document\n\nStart writing in Markdown here...\n"
                )
              }
              className="group text-left space-y-2 cursor-pointer"
            >
              <div className="h-36 rounded-lg bg-white border border-slate-200 group-hover:border-blue-500 shadow-2xs flex items-center justify-center transition">
                <span className="text-4xl font-light text-blue-600">+</span>
              </div>
              <div className="text-xs font-medium text-slate-800">
                Blank document
              </div>
            </button>

            <button
              type="button"
              onClick={() =>
                void handleCreateDoc(
                  "Engineering Design RFC",
                  "# Engineering Design RFC\n\n## Context & Motivation\nDescribe the core problem and goals.\n\n## Proposed Architecture\n- Service boundary\n- Data model\n\n## Open Questions\n- [ ] Capacity planning\n"
                )
              }
              className="group text-left space-y-2 cursor-pointer"
            >
              <div className="h-36 rounded-lg bg-white border border-slate-200 group-hover:border-blue-500 shadow-2xs p-4 flex flex-col justify-between transition">
                <div className="space-y-1.5">
                  <div className="h-2 w-16 rounded bg-blue-600" />
                  <div className="h-1.5 w-full rounded bg-slate-200" />
                  <div className="h-1.5 w-4/5 rounded bg-slate-200" />
                </div>
                <span className="text-[10px] font-medium text-slate-400">
                  Template
                </span>
              </div>
              <div className="text-xs font-medium text-slate-800">
                Engineering RFC
              </div>
            </button>

            <button
              type="button"
              onClick={() =>
                void handleCreateDoc(
                  "Team Weekly Sync Notes",
                  "# Team Weekly Sync Notes\n\n## Updates\n- **Product**: \n- **Engineering**: \n\n## Action Items\n- [ ] Follow up on launch checklist\n"
                )
              }
              className="group text-left space-y-2 cursor-pointer"
            >
              <div className="h-36 rounded-lg bg-white border border-slate-200 group-hover:border-blue-500 shadow-2xs p-4 flex flex-col justify-between transition">
                <div className="space-y-1.5">
                  <div className="h-2 w-20 rounded bg-emerald-600" />
                  <div className="h-1.5 w-full rounded bg-slate-200" />
                  <div className="h-1.5 w-3/4 rounded bg-slate-200" />
                </div>
                <span className="text-[10px] font-medium text-slate-400">
                  Template
                </span>
              </div>
              <div className="text-xs font-medium text-slate-800">
                Meeting Notes
              </div>
            </button>
          </div>
        </div>
      </section>

      {/* Accessible Documents List */}
      <main className="flex-1 max-w-5xl w-full mx-auto py-8 px-6 space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-800">
            Accessible Documents ({filteredDocs.length})
          </h2>
          <span className="text-xs text-slate-500">
            Strict invite-only visibility via <code className="text-blue-600">user_docs</code>
          </span>
        </div>

        {docsLoading ? (
          <div className="text-xs text-slate-400 py-8 text-center">
            Loading documents...
          </div>
        ) : filteredDocs.length === 0 ? (
          <div className="rounded-xl bg-white border border-slate-200 p-10 text-center space-y-2">
            <div className="text-sm font-medium text-slate-700">
              No documents shared with @{currentUser.username} yet
            </div>
            <p className="text-xs text-slate-500">
              Create a new blank document above, or sign in as{" "}
              <strong>Alex Rivera</strong> to share the starter document with{" "}
              {currentUser.full_name}.
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {filteredDocs.map((d) => (
              <div
                key={d.id}
                onClick={() => setActiveDocId(d.id)}
                className="group rounded-xl bg-white border border-slate-200 hover:border-blue-400 p-5 shadow-2xs hover:shadow-md transition cursor-pointer flex flex-col justify-between gap-4"
              >
                <div className="space-y-2">
                  <div className="flex items-start justify-between gap-2">
                    <h3 className="text-base font-semibold text-slate-900 group-hover:text-blue-600 transition">
                      {d.title}
                    </h3>
                    <span
                      className={`shrink-0 rounded-full px-2.5 py-0.5 text-[11px] font-medium ${
                        d.is_owner
                          ? "bg-blue-50 text-blue-700"
                          : "bg-slate-100 text-slate-600"
                      }`}
                    >
                      {d.is_owner ? "Owner" : "Contributor"}
                    </span>
                  </div>
                  <p className="text-xs text-slate-500 line-clamp-2">
                    {markdownToPlainText(d.preview) || "Empty document"}
                  </p>
                </div>

                <div className="flex items-center justify-between pt-3 border-t border-slate-100">
                  <div className="flex items-center gap-2">
                    <div className="flex -space-x-1.5">
                      {d.collaborators.slice(0, 6).map((c) => (
                        <div
                          key={c.user_id}
                          title={`${c.full_name} (cursor: ${c.cursor_pos})`}
                          className="h-6 w-6 rounded-full flex items-center justify-center text-[10px] font-bold text-white ring-2 ring-white relative"
                          style={{ backgroundColor: c.avatar_color }}
                        >
                          {c.full_name
                            .split(" ")
                            .map((n) => n[0])
                            .join("")
                            .slice(0, 2)}
                          {c.is_editing && (
                            <span className="absolute -bottom-0.5 -right-0.5 h-2 w-2 rounded-full bg-emerald-500 ring-1 ring-white" />
                          )}
                        </div>
                      ))}
                    </div>
                    <span className="text-[11px] text-slate-500">
                      {d.collaborators.length} collaborator
                      {d.collaborators.length === 1 ? "" : "s"}
                    </span>
                  </div>

                  <div className="flex items-center gap-2">
                    {d.pending_logs_count > 0 && (
                      <span className="rounded bg-amber-50 px-2 py-0.5 text-[10px] font-medium text-amber-700">
                        Autosave pending
                      </span>
                    )}
                    {d.is_owner && (
                      <button
                        type="button"
                        onClick={(e) => void handleDeleteDoc(e, d.id)}
                        className="rounded px-2 py-1 text-xs text-slate-400 hover:bg-rose-50 hover:text-rose-600 transition cursor-pointer"
                        title="Delete document"
                      >
                        Delete
                      </button>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </main>
    </div>
  );
}

export default App;
