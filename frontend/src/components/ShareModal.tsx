import { useState } from "react";
import {
  addContributor,
  removeContributor,
  type Collaborator,
  type User,
} from "../api/client";

interface ShareModalProps {
  docId: number;
  docTitle: string;
  isOwner: boolean;
  currentUser: User;
  collaborators: Collaborator[];
  allUsers: User[];
  token: string;
  onClose: () => void;
  onCollaboratorsUpdated: (collabs: Collaborator[]) => void;
}

export function ShareModal({
  docId,
  docTitle,
  isOwner,
  currentUser,
  collaborators,
  allUsers,
  token,
  onClose,
  onCollaboratorsUpdated,
}: ShareModalProps) {
  const [selectedUserId, setSelectedUserId] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const existingIds = new Set(collaborators.map((c) => c.user_id));
  const availableUsers = allUsers.filter((u) => !existingIds.has(u.id));

  const handleAdd = async () => {
    if (!selectedUserId) return;
    setBusy(true);
    setError(null);
    try {
      const res = await addContributor(token, docId, Number(selectedUserId));
      onCollaboratorsUpdated(res.collaborators);
      setSelectedUserId("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to add contributor");
    } finally {
      setBusy(false);
    }
  };

  const handleRemove = async (targetUserId: number) => {
    setBusy(true);
    setError(null);
    try {
      const res = await removeContributor(token, docId, targetUserId);
      onCollaboratorsUpdated(res.collaborators);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to remove contributor"
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 backdrop-blur-xs p-4">
      <div className="w-full max-w-lg rounded-2xl bg-white shadow-2xl border border-slate-200 overflow-hidden">
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100">
          <div>
            <h2 className="text-lg font-semibold text-slate-900">
              Share &ldquo;{docTitle}&rdquo;
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Strict invite-only access via <code className="text-blue-600">user_docs</code> table
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-full p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-600 transition cursor-pointer"
          >
            ✕
          </button>
        </div>

        <div className="p-6 space-y-5">
          {error && (
            <div className="rounded-lg bg-rose-50 border border-rose-200 px-3 py-2 text-xs text-rose-700">
              {error}
            </div>
          )}

          {isOwner ? (
            <div className="space-y-2">
              <label className="block text-xs font-medium text-slate-700">
                Invite contributor from the 20 users
              </label>
              <div className="flex gap-2">
                <select
                  value={selectedUserId}
                  onChange={(e) => setSelectedUserId(e.target.value)}
                  className="flex-1 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 focus:border-blue-600 focus:outline-hidden"
                >
                  <option value="">Select a user to invite...</option>
                  {availableUsers.map((u) => (
                    <option key={u.id} value={u.id}>
                      {u.full_name} (@{u.username})
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  disabled={!selectedUserId || busy}
                  onClick={() => void handleAdd()}
                  className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50 transition cursor-pointer"
                >
                  Add
                </button>
              </div>
            </div>
          ) : (
            <div className="rounded-lg bg-slate-50 border border-slate-200 px-3.5 py-2.5 text-xs text-slate-600">
              You are a <strong>Contributor</strong> on this document. Only the{" "}
              <strong>Owner</strong> can invite or remove contributors.
            </div>
          )}

          <div className="space-y-2">
            <div className="flex items-center justify-between text-xs font-medium text-slate-500">
              <span>People with access ({collaborators.length})</span>
              <span>userDocs State (Pointer &amp; Active Lock)</span>
            </div>

            <div className="max-h-64 overflow-y-auto divide-y divide-slate-100 rounded-xl border border-slate-200">
              {collaborators.map((c) => (
                <div
                  key={c.user_id}
                  className="flex items-center justify-between px-3.5 py-2.5 bg-white hover:bg-slate-50/70"
                >
                  <div className="flex items-center gap-3 min-w-0">
                    <div
                      className="h-8 w-8 rounded-full flex items-center justify-center text-xs font-bold text-white shrink-0 relative"
                      style={{ backgroundColor: c.avatar_color }}
                    >
                      {c.full_name
                        .split(" ")
                        .map((n) => n[0])
                        .join("")
                        .slice(0, 2)}
                      {c.is_editing && (
                        <span className="absolute -bottom-0.5 -right-0.5 h-2.5 w-2.5 rounded-full bg-emerald-500 ring-2 ring-white" />
                      )}
                    </div>
                    <div className="truncate">
                      <div className="text-sm font-medium text-slate-900 flex items-center gap-1.5">
                        <span className="truncate">{c.full_name}</span>
                        {c.user_id === currentUser.id && (
                          <span className="text-[11px] text-slate-400">(you)</span>
                        )}
                      </div>
                      <div className="text-xs text-slate-500 flex items-center gap-2">
                        <span>@{c.username}</span>
                        <span>•</span>
                        <span className="font-mono text-[11px] text-slate-600">
                          cursor_pos: {c.cursor_pos}
                        </span>
                        <span
                          className={`inline-flex items-center rounded-full px-1.5 py-0.5 text-[10px] font-medium ${
                            c.is_editing
                              ? "bg-emerald-50 text-emerald-700 ring-1 ring-emerald-600/20"
                              : "bg-slate-100 text-slate-500"
                          }`}
                        >
                          {c.is_editing
                            ? `editing [${c.perimeter_start}..${c.perimeter_end}]`
                            : "idle"}
                        </span>
                      </div>
                    </div>
                  </div>

                  <div className="flex items-center gap-2 shrink-0">
                    {c.is_owner ? (
                      <span className="rounded-md bg-blue-50 px-2.5 py-1 text-xs font-medium text-blue-700">
                        Owner
                      </span>
                    ) : (
                      <>
                        <span className="text-xs text-slate-500">Contributor</span>
                        {isOwner && (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => void handleRemove(c.user_id)}
                            className="ml-1 rounded-md px-2 py-1 text-xs text-rose-600 hover:bg-rose-50 transition cursor-pointer"
                          >
                            Remove
                          </button>
                        )}
                      </>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>

        <div className="flex justify-end bg-slate-50 px-6 py-3 border-t border-slate-100">
          <button
            type="button"
            onClick={onClose}
            className="rounded-full bg-blue-600 px-5 py-1.5 text-sm font-medium text-white hover:bg-blue-700 transition cursor-pointer"
          >
            Done
          </button>
        </div>
      </div>
    </div>
  );
}
