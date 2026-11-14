import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  fetchDocDetail,
  flushDocLogs,
  getDocWebSocketUrl,
  submitPerimeterUpdateHttp,
  updateCursorOrSelectionHttp,
  updateDocTitle,
  type Collaborator,
  type DocDetail,
  type User,
} from "../api/client";
import {
  applyRemoteSplice,
  computeTextSplice,
  markdownToHtml,
  serializeDomWithSelection,
} from "../utils/markdown";
import { ShareModal } from "./ShareModal";

const PERIMETER_WIDTH = 64;
const PERIMETER_HALF = 32;
const BORDER_HYSTERESIS = 18;
const EDIT_DEBOUNCE_MS = 350;

function getPerimeterBounds(cursorPos: number): [number, number] {
  const start = Math.max(0, cursorPos - PERIMETER_HALF);
  return [start, start + PERIMETER_WIDTH];
}

function perimetersOverlap(
  s1: number,
  e1: number,
  s2: number,
  e2: number
): boolean {
  return Math.max(s1, s2) < Math.min(e1, e2);
}

/**
 * 18-char border hysteresis check:
 * Do NOT request a perimeter update until the user's cursor is < 18 chars
 * from the border of their current 64-char perimeter, or outside of it altogether.
 */
function shouldRequestPerimeterShift(
  pStart: number,
  pEnd: number,
  liveCursor: number,
  isCurrentlyEditing: boolean
): boolean {
  if (!isCurrentlyEditing) return true;
  if (liveCursor < pStart || liveCursor > pEnd) return true;
  if (pEnd - liveCursor < BORDER_HYSTERESIS) return true;
  if (pStart > 0 && liveCursor - pStart < BORDER_HYSTERESIS) return true;
  return false;
}

interface DocEditorProps {
  docId: number;
  currentUser: User;
  allUsers: User[];
  token: string;
  onBack: () => void;
}

export function DocEditor({
  docId,
  currentUser,
  allUsers,
  token,
  onBack,
}: DocEditorProps) {
  const [doc, setDoc] = useState<DocDetail | null>(null);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [collaborators, setCollaborators] = useState<Collaborator[]>([]);
  const [pendingLogsCount, setPendingLogsCount] = useState(0);
  const [liveCursor, setLiveCursor] = useState(0);
  const [liveSelection, setLiveSelection] = useState<[number, number] | null>(
    null
  );
  const [editingDisabledBySelection, setEditingDisabledBySelection] =
    useState(false);
  const [debouncingLocal, setDebouncingLocal] = useState(false);
  const [showShareModal, setShowShareModal] = useState(false);
  const [blockedAlert, setBlockedAlert] = useState<string | null>(null);
  const [lastFlushReason, setLastFlushReason] = useState<string | null>(null);
  const [wsConnected, setWsConnected] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [flushCountdown, setFlushCountdown] = useState<number | null>(null);
  const [lastEditTick, setLastEditTick] = useState(0);

  const editorRef = useRef<HTMLDivElement | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  // Canonical base Markdown in docs.content & current working Markdown
  const baseMarkdownRef = useRef<string>("");
  const remoteSyncedMdRef = useRef<string>("");
  const contentRef = useRef<string>("");
  const collaboratorsRef = useRef<Collaborator[]>([]);
  const lastValidHtmlRef = useRef<string>("");
  const liveCursorRef = useRef<number>(0);
  const hadSelectionRef = useRef<boolean>(false);

  const debounceTimerRef = useRef<number | null>(null);
  const hasUnsentDebounceRef = useRef<boolean>(false);
  const isProgrammaticDomUpdateRef = useRef<boolean>(false);

  contentRef.current = content;
  liveCursorRef.current = liveCursor;
  collaboratorsRef.current = collaborators;

  const myCollab = useMemo(
    () => collaborators.find((c) => c.user_id === currentUser.id) ?? null,
    [collaborators, currentUser.id]
  );

  // Render Markdown into the rich-text DOM when loaded or updated remotely
  const applyMarkdownToRichDom = useCallback((md: string) => {
    const html = markdownToHtml(md);
    lastValidHtmlRef.current = html;
    if (editorRef.current && editorRef.current.innerHTML !== html) {
      isProgrammaticDomUpdateRef.current = true;
      editorRef.current.innerHTML = html;
      queueMicrotask(() => {
        isProgrammaticDomUpdateRef.current = false;
      });
    }
  }, []);

  // Flush any pending debounced perimeter text update to the backend
  const flushDebouncedPerimeterEdit = useCallback(
    async (
      overrideMarkdown?: string,
      overrideCursor?: number,
      selectionCollapsed = true,
      flushImmediately = false,
      keepalive = false
    ) => {
      if (debounceTimerRef.current !== null) {
        window.clearTimeout(debounceTimerRef.current);
        debounceTimerRef.current = null;
      }
      if (
        !hasUnsentDebounceRef.current &&
        overrideMarkdown === undefined &&
        !flushImmediately
      ) {
        return;
      }
      hasUnsentDebounceRef.current = false;
      setDebouncingLocal(false);

      const currentMd = overrideMarkdown ?? contentRef.current;
      const refMd = remoteSyncedMdRef.current || baseMarkdownRef.current;
      const baseMd = baseMarkdownRef.current;
      const cursor = overrideCursor ?? liveCursorRef.current;

      const latestCollabs = collaboratorsRef.current;
      const mine = latestCollabs.find((c) => c.user_id === currentUser.id);

      const priorDelta =
        mine && typeof mine.base_perimeter_start === "number"
          ? mine.perimeter_start - mine.base_perimeter_start
          : 0;
      const basePStart =
        mine?.base_perimeter_start ??
        Math.max(0, (mine?.perimeter_start ?? cursor - PERIMETER_HALF) - priorDelta);
      const basePEnd =
        mine?.base_perimeter_end ?? basePStart + PERIMETER_WIDTH;

      // Compute splice strictly against remoteSyncedMdRef (which has base_content + other users' edits,
      // but NOT our own unflushed perimeter edits)
      const liveSplice = computeTextSplice(refMd, currentMd);
      const refPStart = Math.max(0, Math.min(basePStart + priorDelta, refMd.length));
      const refPEnd = Math.max(
        refPStart,
        Math.min(basePEnd + priorDelta, refMd.length)
      );

      const liveUStart = Math.min(refPStart, liveSplice.pos_start);
      const liveUEnd = Math.max(refPEnd, liveSplice.pos_end);
      const suffixLen = Math.max(0, refMd.length - liveUEnd);
      const replacementSliceEnd = Math.max(
        liveUStart,
        currentMd.length - suffixLen
      );
      const replacementText = currentMd.slice(liveUStart, replacementSliceEnd);

      // Map [liveUStart .. liveUEnd] in refMd back to canonical base coordinates in docs.content
      const uStart = Math.max(
        0,
        Math.min(liveUStart - priorDelta, baseMd.length)
      );
      const uEnd = Math.max(
        uStart,
        Math.min(liveUEnd - priorDelta, baseMd.length)
      );

      try {
        const res = await submitPerimeterUpdateHttp(
          token,
          docId,
          {
            pos_start: uStart,
            pos_end: uEnd,
            replacement_text: replacementText,
            full_content: currentMd,
            cursor_pos: cursor,
            selection_collapsed: selectionCollapsed,
            flush_immediately: flushImmediately,
          },
          keepalive
        );

        if (res.status === "blocked") {
          setBlockedAlert(res.message ?? "Edit blocked by active perimeter");
          const fresh = await fetchDocDetail(token, docId);
          baseMarkdownRef.current = fresh.base_content;
          remoteSyncedMdRef.current = fresh.content;
          setContent(fresh.content);
          applyMarkdownToRichDom(fresh.content);
          setCollaborators(fresh.collaborators);
          collaboratorsRef.current = fresh.collaborators;
          setPendingLogsCount(fresh.pending_logs_count);
          return;
        }

        setBlockedAlert(null);
        if (typeof res.base_content === "string") {
          baseMarkdownRef.current = res.base_content;
        }
        if (res.flushed_on_perimeter_shift) {
          baseMarkdownRef.current = res.content;
          remoteSyncedMdRef.current = res.content;
          setPendingLogsCount(0);
          setLastFlushReason("Autosaved");
        } else {
          setPendingLogsCount(res.pending_logs_count);
          setLastEditTick((t) => t + 1);
        }
        if (Array.isArray(res.collaborators)) {
          setCollaborators(res.collaborators);
          collaboratorsRef.current = res.collaborators;
        }
      } catch {
        // ignore network transient error
      }
    },
    [applyMarkdownToRichDom, collaborators, currentUser.id, docId, token]
  );

  // Ensure any unsaved debounced edits are flushed if the user reloads or closes the tab
  useEffect(() => {
    const handleBeforeUnload = () => {
      if (hasUnsentDebounceRef.current && editorRef.current) {
        const { markdown, selStart } = serializeDomWithSelection(
          editorRef.current
        );
        void flushDebouncedPerimeterEdit(markdown, selStart, true, true, true);
      } else if (pendingLogsCount > 0) {
        void fetch(`/api/docs/${docId}/flush`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${token}`,
          },
          keepalive: true,
        });
      }
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, [docId, flushDebouncedPerimeterEdit, pendingLogsCount, token]);

  // Check if current cursor or selection overlaps any OTHER active collaborator's perimeter
  const overlappingOther = useMemo(() => {
    const [candStart, candEnd] = liveSelection
      ? liveSelection
      : getPerimeterBounds(liveCursor);
    for (const c of collaborators) {
      if (c.user_id !== currentUser.id && c.is_editing) {
        if (
          perimetersOverlap(
            candStart,
            candEnd,
            c.perimeter_start,
            c.perimeter_end
          )
        ) {
          return c;
        }
      }
    }
    return null;
  }, [collaborators, currentUser.id, liveCursor, liveSelection]);

  // Load initial document state
  const loadDocument = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const detail = await fetchDocDetail(token, docId);
      setDoc(detail);
      setTitle(detail.title);
      baseMarkdownRef.current = detail.base_content;
      let syncedWithoutMine = detail.base_content;
      const otherLogs = (detail.recent_logs ?? [])
        .filter((lg) => lg.user_id !== currentUser.id)
        .sort((a, b) => b.pos_start - a.pos_start);
      for (const lg of otherLogs) {
        const s = Math.max(0, Math.min(lg.pos_start, syncedWithoutMine.length));
        const e = Math.max(s, Math.min(lg.pos_end, syncedWithoutMine.length));
        syncedWithoutMine =
          syncedWithoutMine.slice(0, s) +
          lg.inserted_text +
          syncedWithoutMine.slice(e);
      }
      remoteSyncedMdRef.current = syncedWithoutMine;
      setContent(detail.content);
      setCollaborators(detail.collaborators);
      collaboratorsRef.current = detail.collaborators;
      setPendingLogsCount(detail.pending_logs_count);
      const mine = detail.collaborators.find(
        (c) => c.user_id === currentUser.id
      );
      if (mine) {
        setLiveCursor(mine.cursor_pos);
        setEditingDisabledBySelection(
          Boolean(mine.editing_disabled_by_selection)
        );
      }
      requestAnimationFrame(() => {
        applyMarkdownToRichDom(detail.content);
      });
    } catch (err) {
      const msg =
        err instanceof Error ? err.message : "Failed to load document";
      if (msg.toLowerCase().includes("not found")) {
        onBack();
        return;
      }
      setLoadError(msg);
    } finally {
      setLoading(false);
    }
  }, [applyMarkdownToRichDom, currentUser.id, docId, onBack, token]);

  useEffect(() => {
    void loadDocument();
  }, [loadDocument]);

  // Populate rich text DOM once editorRef mounts
  useEffect(() => {
    if (!loading && doc && editorRef.current && !editorRef.current.innerHTML) {
      applyMarkdownToRichDom(contentRef.current);
    }
  }, [applyMarkdownToRichDom, doc, loading]);

  // 20-second inactivity countdown visual ticker when pending logs > 0
  useEffect(() => {
    if (pendingLogsCount <= 0) {
      setFlushCountdown(null);
      return;
    }
    setFlushCountdown(20);
    const interval = window.setInterval(() => {
      setFlushCountdown((prev) => (prev !== null && prev > 0 ? prev - 1 : 0));
    }, 1000);
    return () => window.clearInterval(interval);
  }, [pendingLogsCount, lastEditTick]);

  // Trigger Autosave immediately when the 20s inactivity timer expires (reaches 0)
  useEffect(() => {
    if (flushCountdown !== 0 || pendingLogsCount <= 0) return;
    setFlushCountdown(null);
    void flushDocLogs(token, docId)
      .then((res) => {
        baseMarkdownRef.current = res.content;
        remoteSyncedMdRef.current = res.content;
        setPendingLogsCount(0);
        if (res.collaborators) {
          setCollaborators(res.collaborators);
          collaboratorsRef.current = res.collaborators;
        }
        setLastFlushReason("Autosaved");
      })
      .catch(() => {
        // ignore
      });
  }, [docId, flushCountdown, pendingLogsCount, token]);

  // Connect WebSocket (/api/ws/docs/{docId})
  useEffect(() => {
    const wsUrl = getDocWebSocketUrl(docId, token);
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      setWsConnected(true);
    };

    ws.onclose = () => {
      setWsConnected(false);
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data as string) as Record<string, unknown>;
        const msgType = msg.type as string;

        if (msgType === "collaborators_updated") {
          if (Array.isArray(msg.collaborators)) {
            setCollaborators(msg.collaborators as Collaborator[]);
          }
        } else if (msgType === "cursor_ack") {
          if (Array.isArray(msg.collaborators)) {
            setCollaborators(msg.collaborators as Collaborator[]);
          }
          if (msg.status === "selection_read_only") {
            setEditingDisabledBySelection(true);
            if (typeof msg.message === "string") {
              setBlockedAlert(msg.message);
            }
          } else if (msg.status === "blocked") {
            if (typeof msg.message === "string") {
              setBlockedAlert(msg.message);
            }
          } else if (
            msg.status === "ok" ||
            msg.status === "kept_existing_perimeter" ||
            msg.status === "selection_locked"
          ) {
            setEditingDisabledBySelection(false);
            setBlockedAlert(null);
          }
          if (msg.flushed) {
            baseMarkdownRef.current = contentRef.current;
            setPendingLogsCount(0);
            setLastFlushReason("Autosaved");
          }
        } else if (msgType === "perimeter_edit_applied") {
          if (typeof msg.pending_logs_count === "number") {
            setPendingLogsCount(msg.pending_logs_count);
            if (msg.pending_logs_count > 0) {
              setLastEditTick((t) => t + 1);
            }
          }
          if (typeof msg.base_content === "string") {
            baseMarkdownRef.current = msg.base_content;
          }
          const authorId = msg.user_id as number;
          if (authorId !== currentUser.id) {
            const posStart = msg.pos_start as number;
            const posEnd = msg.pos_end as number;
            const insertedText = msg.inserted_text as string;
            const { text: updatedRemote } = applyRemoteSplice(
              remoteSyncedMdRef.current,
              posStart,
              posEnd,
              insertedText,
              0
            );
            remoteSyncedMdRef.current = updatedRemote;
            const { text: updatedText, nextCursor } = applyRemoteSplice(
              contentRef.current,
              posStart,
              posEnd,
              insertedText,
              liveCursorRef.current
            );
            setContent(updatedText);
            contentRef.current = updatedText;
            setLiveCursor(nextCursor);
            applyMarkdownToRichDom(updatedText);
          }
          if (Array.isArray(msg.collaborators)) {
            setCollaborators(msg.collaborators as Collaborator[]);
            collaboratorsRef.current = msg.collaborators as Collaborator[];
          }
        } else if (msgType === "doc_flushed") {
          setPendingLogsCount(0);
          if (typeof msg.content === "string") {
            baseMarkdownRef.current = msg.content;
            remoteSyncedMdRef.current = msg.content;
            if (!hasUnsentDebounceRef.current) {
              setContent(msg.content);
              contentRef.current = msg.content;
              if (
                msg.triggered_by_user_id &&
                msg.triggered_by_user_id !== currentUser.id
              ) {
                applyMarkdownToRichDom(msg.content);
              }
            }
          }
          if (Array.isArray(msg.collaborators)) {
            setCollaborators(msg.collaborators as Collaborator[]);
            collaboratorsRef.current = msg.collaborators as Collaborator[];
          }
          const reason = msg.reason as string;
          setLastFlushReason(
            reason === "manual_flush" ? "Saved manually" : "Autosaved"
          );
        } else if (msgType === "title_updated") {
          if (typeof msg.title === "string") {
            setTitle(msg.title);
          }
        }
      } catch {
        // ignore malformed message
      }
    };

    return () => {
      if (debounceTimerRef.current !== null) {
        window.clearTimeout(debounceTimerRef.current);
      }
      ws.close();
      wsRef.current = null;
    };
  }, [applyMarkdownToRichDom, currentUser.id, docId, token]);

  // Evaluate cursor or selection changes from the rich-text DOM
  const evaluateDomCursorOrSelection = useCallback(() => {
    const el = editorRef.current;
    if (!el) return;
    const { markdown, selStart, selEnd } = serializeDomWithSelection(el);
    setLiveCursor(selStart);
    liveCursorRef.current = selStart;

    // 1. User selected a chunk of text (selEnd > selStart)
    if (selEnd > selStart) {
      hadSelectionRef.current = true;
      setLiveSelection([selStart, selEnd]);

      if (
        myCollab?.is_selection_perimeter &&
        myCollab.perimeter_start === selStart &&
        myCollab.perimeter_end === selEnd
      ) {
        return;
      }

      if (hasUnsentDebounceRef.current) {
        void flushDebouncedPerimeterEdit(markdown, selStart, false, true);
      }

      let overlapsOther: Collaborator | null = null;
      for (const c of collaborators) {
        if (c.user_id !== currentUser.id && c.is_editing) {
          if (
            perimetersOverlap(
              selStart,
              selEnd,
              c.perimeter_start,
              c.perimeter_end
            )
          ) {
            overlapsOther = c;
            break;
          }
        }
      }

      if (overlapsOther) {
        setEditingDisabledBySelection(true);
        setBlockedAlert(
          `Selection includes ${overlapsOther.full_name}'s active perimeter. Stored as expired perimeter — Copy (Cmd/Ctrl+C) is allowed, editing is disabled until you deselect.`
        );
      } else {
        setEditingDisabledBySelection(false);
        setBlockedAlert(null);
      }

      void updateCursorOrSelectionHttp(token, docId, {
        cursor_pos: selStart,
        selection_start: selStart,
        selection_end: selEnd,
        is_editing: true,
      }).then((res) => {
        if (res.collaborators) setCollaborators(res.collaborators);
        if (res.flushed) {
          baseMarkdownRef.current = contentRef.current;
          setPendingLogsCount(0);
          setLastFlushReason("Autosaved");
        }
      });
      return;
    }

    // 2. Deselected or Normal Cursor (selStart === selEnd)
    const justDeselected =
      hadSelectionRef.current ||
      liveSelection !== null ||
      Boolean(myCollab?.is_selection_perimeter);

    hadSelectionRef.current = false;
    setLiveSelection(null);

    if (justDeselected) {
      setEditingDisabledBySelection(false);
      // Rule: Deselection ALWAYS triggers an immediate Autosave!
      if (hasUnsentDebounceRef.current) {
        void flushDebouncedPerimeterEdit(markdown, selStart, true, true);
      }
      void updateCursorOrSelectionHttp(token, docId, {
        cursor_pos: selStart,
        selection_start: null,
        selection_end: null,
        is_editing: true,
      }).then((res) => {
        if (res.collaborators) setCollaborators(res.collaborators);
        baseMarkdownRef.current = contentRef.current;
        setPendingLogsCount(0);
        setLastFlushReason("Autosaved");
      });
      return;
    }

    // Check if candidate 64-char perimeter overlaps another active user
    const [candStart, candEnd] = getPerimeterBounds(selStart);
    for (const c of collaborators) {
      if (c.user_id !== currentUser.id && c.is_editing) {
        if (
          perimetersOverlap(
            candStart,
            candEnd,
            c.perimeter_start,
            c.perimeter_end
          )
        ) {
          setBlockedAlert(
            `Blocked: ${c.full_name} holds active perimeter [${c.perimeter_start}..${c.perimeter_end}]. Your 64-char window [${candStart}..${candEnd}] cannot overlap.`
          );
          return;
        }
      }
    }

    setBlockedAlert(null);

    const livePStart = myCollab?.perimeter_start ?? candStart;
    const livePEnd = myCollab?.perimeter_end ?? candEnd;
    const isEditingNow = Boolean(myCollab?.is_editing);

    // 18-char border hysteresis check
    if (shouldRequestPerimeterShift(livePStart, livePEnd, selStart, isEditingNow)) {
      if (hasUnsentDebounceRef.current) {
        void flushDebouncedPerimeterEdit(markdown, selStart, true, true);
      }
      void updateCursorOrSelectionHttp(token, docId, {
        cursor_pos: selStart,
        selection_start: null,
        selection_end: null,
        is_editing: true,
      }).then((res) => {
        if (res.collaborators) setCollaborators(res.collaborators);
        if (res.flushed) {
          baseMarkdownRef.current = contentRef.current;
          setPendingLogsCount(0);
          setLastFlushReason("Autosaved");
        }
      });
    }
  }, [
    collaborators,
    currentUser.id,
    docId,
    flushDebouncedPerimeterEdit,
    liveSelection,
    myCollab,
    token,
  ]);

  // Handle Rich Text edits from any source (typing, paste, spellcheck, extensions, or direct DOM mutations)
  const handleRichEditorInput = useCallback(() => {
    const el = editorRef.current;
    if (!el || isProgrammaticDomUpdateRef.current) return;

    const { markdown, selStart, selEnd, hasSelectionInRoot } =
      serializeDomWithSelection(el);

    // Ignore non-text DOM mutations if the serialized visible content did not change
    if (markdown === contentRef.current) {
      return;
    }

    // 1. Block edits if selection overlapped another collaborator's active perimeter
    if (editingDisabledBySelection) {
      isProgrammaticDomUpdateRef.current = true;
      el.innerHTML = lastValidHtmlRef.current;
      queueMicrotask(() => {
        isProgrammaticDomUpdateRef.current = false;
      });
      setBlockedAlert(
        "Editing is disabled while your selection overlaps another collaborator's perimeter. Copy (Cmd/Ctrl+C) is allowed; deselect to edit."
      );
      return;
    }

    const diffSplice = computeTextSplice(contentRef.current, markdown);
    const inferredEditCursor =
      diffSplice.pos_start + diffSplice.inserted_text.length;
    const effectiveCursor =
      !hasSelectionInRoot ||
      Math.abs(selStart - inferredEditCursor) > PERIMETER_HALF
        ? inferredEditCursor
        : selStart;
    const selectionCollapsed = !hasSelectionInRoot || selStart === selEnd;

    // 2. Check overlap against other active collaborators
    const [candStart, candEnd] = getPerimeterBounds(effectiveCursor);
    for (const c of collaborators) {
      if (c.user_id !== currentUser.id && c.is_editing) {
        if (
          perimetersOverlap(
            candStart,
            candEnd,
            c.perimeter_start,
            c.perimeter_end
          )
        ) {
          isProgrammaticDomUpdateRef.current = true;
          el.innerHTML = lastValidHtmlRef.current;
          queueMicrotask(() => {
            isProgrammaticDomUpdateRef.current = false;
          });
          setBlockedAlert(
            `Edit forbidden: overlaps ${c.full_name}'s locked 64-char perimeter [${c.perimeter_start}..${c.perimeter_end}].`
          );
          return;
        }
      }
    }

    lastValidHtmlRef.current = el.innerHTML;
    setBlockedAlert(null);
    setContent(markdown);
    contentRef.current = markdown;
    setLiveCursor(effectiveCursor);
    liveCursorRef.current = effectiveCursor;

    const wasSelecting =
      hadSelectionRef.current ||
      liveSelection !== null ||
      Boolean(myCollab?.is_selection_perimeter);

    if (selectionCollapsed) {
      hadSelectionRef.current = false;
      setLiveSelection(null);
    }

    hasUnsentDebounceRef.current = true;
    setDebouncingLocal(true);

    const livePStart = myCollab?.perimeter_start ?? candStart;
    const livePEnd = myCollab?.perimeter_end ?? candEnd;
    const wasSelectionCutOrDelete = wasSelecting && selectionCollapsed;
    const crossedHysteresis = shouldRequestPerimeterShift(
      livePStart,
      livePEnd,
      effectiveCursor,
      Boolean(myCollab?.is_editing)
    );

    if (wasSelectionCutOrDelete || crossedHysteresis) {
      // Flush immediately so deselection or perimeter border crossing triggers an Autosave!
      void flushDebouncedPerimeterEdit(
        markdown,
        effectiveCursor,
        selectionCollapsed,
        true
      );
    } else {
      if (debounceTimerRef.current !== null) {
        window.clearTimeout(debounceTimerRef.current);
      }
      debounceTimerRef.current = window.setTimeout(() => {
        void flushDebouncedPerimeterEdit(
          contentRef.current,
          liveCursorRef.current,
          true,
          false
        );
      }, EDIT_DEBOUNCE_MS);
    }
  }, [
    collaborators,
    currentUser.id,
    editingDisabledBySelection,
    flushDebouncedPerimeterEdit,
    liveSelection,
    myCollab,
  ]);

  // Observe any direct DOM changes to visible text inside the editor (regardless of source)
  useEffect(() => {
    const el = editorRef.current;
    if (!el || loading) return;
    const observer = new MutationObserver(() => {
      if (isProgrammaticDomUpdateRef.current) return;
      handleRichEditorInput();
    });
    observer.observe(el, {
      subtree: true,
      childList: true,
      characterData: true,
    });
    return () => observer.disconnect();
  }, [doc, handleRichEditorInput, loading]);

  // Execute rich-text formatting command (Google Docs style — zero visible Markdown)
  const applyRichFormatting = (command: string, value?: string) => {
    const el = editorRef.current;
    if (!el) return;
    if (editingDisabledBySelection || overlappingOther) {
      setBlockedAlert(
        editingDisabledBySelection
          ? "Editing disabled until you deselect the overlapping selection (Copy is allowed)."
          : `Edit forbidden: overlaps ${overlappingOther?.full_name}'s active perimeter.`
      );
      return;
    }
    el.focus();
    document.execCommand(command, false, value);
    handleRichEditorInput();
  };

  const handleTitleBlur = async () => {
    if (!doc || !title.trim() || title.trim() === doc.title) return;
    try {
      const res = await updateDocTitle(token, docId, title.trim());
      setTitle(res.title);
    } catch {
      // ignore
    }
  };

  const handleManualSave = async () => {
    try {
      if (hasUnsentDebounceRef.current && editorRef.current) {
        const { markdown, selStart } = serializeDomWithSelection(
          editorRef.current
        );
        await flushDebouncedPerimeterEdit(markdown, selStart, true, true);
      }
      const res = await flushDocLogs(token, docId);
      baseMarkdownRef.current = res.content;
      remoteSyncedMdRef.current = res.content;
      setPendingLogsCount(0);
      if (res.collaborators) {
        setCollaborators(res.collaborators);
        collaboratorsRef.current = res.collaborators;
      }
      setLastFlushReason("Saved manually");
    } catch {
      // ignore
    }
  };

  if (loading) {
    return (
      <div className="min-h-screen bg-[#F8FAFC] flex items-center justify-center text-slate-500 text-sm">
        Loading document workspace...
      </div>
    );
  }

  if (loadError || !doc) {
    return (
      <div className="min-h-screen bg-[#F8FAFC] flex flex-col items-center justify-center p-6 gap-4">
        <div className="rounded-xl bg-white border border-rose-200 p-6 shadow-sm max-w-md text-center space-y-3">
          <div className="text-rose-600 font-semibold">
            Unable to Open Document
          </div>
          <p className="text-xs text-slate-600">{loadError}</p>
          <button
            type="button"
            onClick={onBack}
            className="rounded-lg bg-blue-600 px-4 py-2 text-xs font-medium text-white hover:bg-blue-700 cursor-pointer"
          >
            Back to Documents
          </button>
        </div>
      </div>
    );
  }

  const myPStart =
    liveSelection?.[0] ??
    myCollab?.perimeter_start ??
    Math.max(0, liveCursor - PERIMETER_HALF);
  const myPEnd =
    liveSelection?.[1] ??
    myCollab?.perimeter_end ??
    myPStart + PERIMETER_WIDTH;
  const distToLeftBorder =
    myPStart > 0 ? Math.max(0, liveCursor - myPStart) : 999;
  const distToRightBorder = Math.max(0, myPEnd - liveCursor);
  const minBorderDist = Math.min(distToLeftBorder, distToRightBorder);
  const totalLen = Math.max(content.length, 256);

  return (
    <div className="min-h-screen bg-[#F1F3F4] flex flex-col text-slate-900">
      {/* Top Google Docs Header Bar */}
      <header className="sticky top-0 z-30 bg-white border-b border-slate-200 px-4 py-2 shadow-2xs">
        <div className="flex items-center justify-between gap-4">
          {/* Left: Doc Icon + Title + Autosave Status */}
          <div className="flex items-center gap-3 min-w-0">
            <button
              type="button"
              onClick={() => {
                void handleManualSave().finally(() => onBack());
              }}
              title="Back to Documents Home"
              className="flex h-10 w-10 items-center justify-center rounded-lg bg-blue-600 text-white hover:bg-blue-700 transition shrink-0 shadow-xs cursor-pointer"
            >
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
            </button>

            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <input
                  type="text"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  onBlur={() => void handleTitleBlur()}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") e.currentTarget.blur();
                  }}
                  className="rounded px-2 py-0.5 text-base font-semibold text-slate-900 hover:border-slate-300 focus:border-blue-600 focus:bg-white border border-transparent focus:outline-hidden truncate max-w-xs sm:max-w-md"
                />
              </div>

              {/* Autosave & Manual Save Status Row */}
              <div className="flex items-center gap-2 px-2 text-xs text-slate-500">
                <span
                  className={`inline-block h-2 w-2 rounded-full ${
                    wsConnected ? "bg-emerald-500" : "bg-amber-500"
                  }`}
                />
                {debouncingLocal ? (
                  <span className="text-blue-600 font-medium">
                    Syncing edits...
                  </span>
                ) : pendingLogsCount > 0 ? (
                  <span className="text-amber-700 font-medium flex items-center gap-2">
                    <span>Autosaving in {flushCountdown ?? 20}s...</span>
                    <button
                      type="button"
                      onClick={() => void handleManualSave()}
                      className="inline-flex items-center gap-1 rounded-md bg-blue-50 px-2 py-0.5 text-[11px] font-semibold text-blue-700 hover:bg-blue-100 border border-blue-200 transition cursor-pointer"
                    >
                      Save now
                    </button>
                  </span>
                ) : (
                  <span className="text-slate-500 flex items-center gap-1">
                    <svg
                      className="h-3.5 w-3.5 text-emerald-600"
                      fill="none"
                      viewBox="0 0 24 24"
                      stroke="currentColor"
                      strokeWidth={2.5}
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        d="M5 13l4 4L19 7"
                      />
                    </svg>
                    {lastFlushReason ?? "All changes saved"}
                  </span>
                )}
              </div>
            </div>
          </div>

          {/* Right: Active Collaborators Avatars + Save + Share Button */}
          <div className="flex items-center gap-3 shrink-0">
            <div className="flex items-center -space-x-1.5">
              {collaborators.map((c) => (
                <div
                  key={c.user_id}
                  title={`${c.full_name} (${c.is_owner ? "Owner" : "Contributor"})\ncursor_pos: ${c.cursor_pos} | perimeter: [${c.perimeter_start}..${c.perimeter_end}]${c.is_selection_perimeter ? " (Selection Perimeter)" : ""}\nstatus: ${c.is_editing ? "ACTIVE EDITING" : c.editing_disabled_by_selection ? "EXPIRED SELECTION (Copy-Only)" : "Idle"}`}
                  className={`relative h-8 w-8 rounded-full flex items-center justify-center text-xs font-bold text-white ring-2 transition ${
                    c.is_editing
                      ? "ring-emerald-500 scale-105 z-10"
                      : c.editing_disabled_by_selection
                        ? "ring-amber-500 opacity-90"
                        : "ring-white opacity-85"
                  }`}
                  style={{ backgroundColor: c.avatar_color }}
                >
                  {c.full_name
                    .split(" ")
                    .map((n) => n[0])
                    .join("")
                    .slice(0, 2)}
                  {c.is_editing && (
                    <span className="absolute -bottom-0.5 -right-0.5 h-2.5 w-2.5 rounded-full bg-emerald-500 ring-2 ring-white animate-pulse" />
                  )}
                </div>
              ))}
            </div>

            <button
              type="button"
              onClick={() => void handleManualSave()}
              title="Manual Save (Cmd/Ctrl+S)"
              className="inline-flex items-center gap-1.5 rounded-full border border-slate-200 bg-white px-3.5 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50 transition cursor-pointer"
            >
              <svg
                className="h-3.5 w-3.5 text-slate-600"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={2}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M5 13l4 4L19 7"
                />
              </svg>
              Save
            </button>

            <button
              type="button"
              onClick={() => setShowShareModal(true)}
              className="inline-flex items-center gap-1.5 rounded-full bg-blue-600 px-4 py-2 text-xs font-semibold text-white shadow-xs hover:bg-blue-700 transition cursor-pointer"
            >
              <svg
                className="h-3.5 w-3.5"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={2}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M12 4.354a4 4 0 110 5.292M15 21H3v-1a6 6 0 0112 0v1zm0 0h6v-1a6 6 0 00-9-5.197M13 7a4 4 0 11-8 0 4 4 0 018 0z"
                />
              </svg>
              Share ({collaborators.length})
            </button>
          </div>
        </div>

        {/* Google Docs WYSIWYG Rich-Text Formatting Toolbar */}
        <div className="mt-2 flex flex-wrap items-center justify-between gap-2 rounded-full bg-[#EDF2FA] px-4 py-1.5 text-xs text-slate-700">
          <div className="flex flex-wrap items-center gap-1">
            <button
              type="button"
              disabled={editingDisabledBySelection}
              onMouseDown={(e) => {
                e.preventDefault();
                applyRichFormatting("undo");
              }}
              className="rounded px-2 py-1 hover:bg-slate-200/80 disabled:opacity-40 transition cursor-pointer"
              title="Undo"
            >
              ↶
            </button>
            <button
              type="button"
              disabled={editingDisabledBySelection}
              onMouseDown={(e) => {
                e.preventDefault();
                applyRichFormatting("redo");
              }}
              className="rounded px-2 py-1 hover:bg-slate-200/80 disabled:opacity-40 transition cursor-pointer"
              title="Redo"
            >
              ↷
            </button>
            <span className="mx-1 h-4 w-px bg-slate-300" />
            <button
              type="button"
              disabled={editingDisabledBySelection}
              onMouseDown={(e) => {
                e.preventDefault();
                applyRichFormatting("formatBlock", "P");
              }}
              className="rounded px-2 py-1 font-medium hover:bg-slate-200/80 disabled:opacity-40 transition cursor-pointer"
              title="Normal text"
            >
              Normal text
            </button>
            <button
              type="button"
              disabled={editingDisabledBySelection}
              onMouseDown={(e) => {
                e.preventDefault();
                applyRichFormatting("formatBlock", "H1");
              }}
              className="rounded px-2 py-1 font-semibold hover:bg-slate-200/80 disabled:opacity-40 transition cursor-pointer"
              title="Title / Heading 1"
            >
              Heading 1
            </button>
            <button
              type="button"
              disabled={editingDisabledBySelection}
              onMouseDown={(e) => {
                e.preventDefault();
                applyRichFormatting("formatBlock", "H2");
              }}
              className="rounded px-2 py-1 font-semibold hover:bg-slate-200/80 disabled:opacity-40 transition cursor-pointer"
              title="Heading 2"
            >
              Heading 2
            </button>
            <button
              type="button"
              disabled={editingDisabledBySelection}
              onMouseDown={(e) => {
                e.preventDefault();
                applyRichFormatting("formatBlock", "H3");
              }}
              className="rounded px-2 py-1 font-semibold hover:bg-slate-200/80 disabled:opacity-40 transition cursor-pointer"
              title="Heading 3"
            >
              Heading 3
            </button>
            <span className="mx-1 h-4 w-px bg-slate-300" />
            <button
              type="button"
              disabled={editingDisabledBySelection}
              onMouseDown={(e) => {
                e.preventDefault();
                applyRichFormatting("bold");
              }}
              className="rounded px-2.5 py-1 font-bold hover:bg-slate-200/80 disabled:opacity-40 transition cursor-pointer"
              title="Bold (Cmd/Ctrl+B)"
            >
              B
            </button>
            <button
              type="button"
              disabled={editingDisabledBySelection}
              onMouseDown={(e) => {
                e.preventDefault();
                applyRichFormatting("italic");
              }}
              className="rounded px-2.5 py-1 italic font-serif hover:bg-slate-200/80 disabled:opacity-40 transition cursor-pointer"
              title="Italic (Cmd/Ctrl+I)"
            >
              I
            </button>
            <button
              type="button"
              disabled={editingDisabledBySelection}
              onMouseDown={(e) => {
                e.preventDefault();
                applyRichFormatting("strikeThrough");
              }}
              className="rounded px-2.5 py-1 line-through hover:bg-slate-200/80 disabled:opacity-40 transition cursor-pointer"
              title="Strikethrough"
            >
              S
            </button>
            <span className="mx-1 h-4 w-px bg-slate-300" />
            <button
              type="button"
              disabled={editingDisabledBySelection}
              onMouseDown={(e) => {
                e.preventDefault();
                applyRichFormatting("insertUnorderedList");
              }}
              className="rounded px-2.5 py-1 hover:bg-slate-200/80 disabled:opacity-40 transition cursor-pointer"
              title="Bulleted list"
            >
              • List
            </button>
            <button
              type="button"
              disabled={editingDisabledBySelection}
              onMouseDown={(e) => {
                e.preventDefault();
                applyRichFormatting("insertOrderedList");
              }}
              className="rounded px-2.5 py-1 hover:bg-slate-200/80 disabled:opacity-40 transition cursor-pointer"
              title="Numbered list"
            >
              1. List
            </button>
            <button
              type="button"
              disabled={editingDisabledBySelection}
              onMouseDown={(e) => {
                e.preventDefault();
                applyRichFormatting("formatBlock", "BLOCKQUOTE");
              }}
              className="rounded px-2.5 py-1 hover:bg-slate-200/80 disabled:opacity-40 transition cursor-pointer"
              title="Quote block"
            >
              &ldquo; Quote
            </button>
          </div>

          {/* Subtle Perimeter Indicator on right side of toolbar */}
          <div className="flex items-center gap-2 text-[11px] text-slate-600">
            <span className="rounded-full bg-white px-2.5 py-0.5 border border-slate-200 font-mono">
              {liveSelection || myCollab?.is_selection_perimeter
                ? `Selection [${myPStart}..${myPEnd}]`
                : `64-char Perimeter [${myPStart}..${myPEnd}]`}
            </span>
            {!liveSelection && !myCollab?.is_selection_perimeter && (
              <span
                className={`rounded-full px-2 py-0.5 font-medium ${
                  minBorderDist < BORDER_HYSTERESIS
                    ? "bg-amber-100 text-amber-800"
                    : "bg-emerald-50 text-emerald-700"
                }`}
              >
                Border: {minBorderDist === 999 ? "start" : `${minBorderDist}c`}
              </span>
            )}
          </div>
        </div>

        {/* Thin Visual Collaborator Perimeter Bar */}
        <div className="mt-1.5 relative h-1.5 w-full rounded-full bg-slate-200 overflow-hidden">
          {collaborators.map((c) => {
            const leftPct = Math.min(100, (c.perimeter_start / totalLen) * 100);
            const widthPct = Math.max(
              2,
              Math.min(
                100 - leftPct,
                ((c.perimeter_end - c.perimeter_start) / totalLen) * 100
              )
            );
            return (
              <div
                key={c.user_id}
                title={`${c.full_name}: [${c.perimeter_start}..${c.perimeter_end}] (${c.is_editing ? "Active Lock" : c.editing_disabled_by_selection ? "Expired Selection (Copy-Only)" : "Idle Pointer"})`}
                style={{
                  left: `${leftPct}%`,
                  width: `${widthPct}%`,
                  backgroundColor: c.avatar_color,
                  opacity: c.is_editing ? 0.9 : 0.3,
                }}
                className="absolute top-0 bottom-0 transition-all duration-150"
              />
            );
          })}
        </div>
      </header>

      {/* Overlap / Expired Selection Banner */}
      {(blockedAlert || editingDisabledBySelection || overlappingOther) && (
        <div
          className={`${
            editingDisabledBySelection ? "bg-amber-600" : "bg-rose-600"
          } text-white px-6 py-2 text-xs font-medium text-center shadow-xs`}
        >
          {blockedAlert ??
            (editingDisabledBySelection
              ? "Your selection includes another collaborator's active perimeter: stored as an expired perimeter. You can Copy (Cmd/Ctrl+C), but editing/cutting is disabled until you deselect."
              : `Race Condition Guard: Overlaps ${overlappingOther?.full_name}'s locked perimeter [${overlappingOther?.perimeter_start}..${overlappingOther?.perimeter_end}]. Move cursor outside or wait a few seconds for inactivity expiry.`)}
        </div>
      )}

      {/* Single Centered Google Docs Rich-Text Page Canvas */}
      <div className="flex-1 overflow-y-auto py-8 px-4 sm:px-6">
        <div className="mx-auto max-w-[816px] bg-white rounded-xs shadow-md border border-slate-200 min-h-[960px] px-12 sm:px-16 py-14">
          <div
            ref={editorRef}
            contentEditable
            suppressContentEditableWarning
            onInput={handleRichEditorInput}
            onMouseUp={evaluateDomCursorOrSelection}
            onKeyUp={(e) => {
              const navOrSelectKeys = [
                "ArrowLeft",
                "ArrowRight",
                "ArrowUp",
                "ArrowDown",
                "Home",
                "End",
                "PageUp",
                "PageDown",
                "Escape",
                "Shift",
              ];
              const sel = window.getSelection();
              const hasActiveSelection = Boolean(sel && !sel.isCollapsed);
              if (
                navOrSelectKeys.includes(e.key) ||
                hasActiveSelection ||
                hadSelectionRef.current
              ) {
                evaluateDomCursorOrSelection();
              }
            }}
            onCut={(e) => {
              if (editingDisabledBySelection) {
                e.preventDefault();
                setBlockedAlert(
                  "Cut blocked: selection overlaps another user's perimeter (Copy Cmd/Ctrl+C is allowed; deselect to edit)."
                );
              }
            }}
            onPaste={(e) => {
              if (editingDisabledBySelection) {
                e.preventDefault();
                setBlockedAlert(
                  "Paste blocked: selection overlaps another user's perimeter. Deselect first."
                );
              }
            }}
            onKeyDown={(e) => {
              if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
                e.preventDefault();
                void handleManualSave();
                return;
              }
              if (!editingDisabledBySelection) return;
              const isCopy =
                (e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "c";
              const isNav = [
                "ArrowLeft",
                "ArrowRight",
                "ArrowUp",
                "ArrowDown",
                "Escape",
                "Home",
                "End",
                "PageUp",
                "PageDown",
                "Shift",
                "Meta",
                "Control",
                "Alt",
              ].includes(e.key);
              if (!isCopy && !isNav) {
                e.preventDefault();
              }
            }}
            className="rich-doc-editor min-h-[840px]"
          />
        </div>
      </div>

      {/* Share / Contributors Modal */}
      {showShareModal && (
        <ShareModal
          docId={docId}
          docTitle={title}
          isOwner={doc.is_owner}
          currentUser={currentUser}
          collaborators={collaborators}
          allUsers={allUsers}
          token={token}
          onClose={() => setShowShareModal(false)}
          onCollaboratorsUpdated={(updated) => setCollaborators(updated)}
        />
      )}
    </div>
  );
}
