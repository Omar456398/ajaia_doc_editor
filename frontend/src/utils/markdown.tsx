import { resolveAttachmentUrl } from "../api/client";

/**
 * Background Markdown <-> Rich Text HTML engine.
 * The database and collaboration protocol operate on Markdown (.md) in the background,
 * while the user sees and edits only formatted Rich Text (WYSIWYG) like Google Docs.
 */

export function computeTextSplice(
  oldText: string,
  newText: string
): { pos_start: number; pos_end: number; inserted_text: string } {
  let start = 0;
  const minLen = Math.min(oldText.length, newText.length);
  while (start < minLen && oldText[start] === newText[start]) {
    start++;
  }

  let oldEnd = oldText.length;
  let newEnd = newText.length;
  while (
    oldEnd > start &&
    newEnd > start &&
    oldText[oldEnd - 1] === newText[newEnd - 1]
  ) {
    oldEnd--;
    newEnd--;
  }

  return {
    pos_start: start,
    pos_end: oldEnd,
    inserted_text: newText.slice(start, newEnd),
  };
}

export function applyRemoteSplice(
  text: string,
  posStart: number,
  posEnd: number,
  insertedText: string,
  currentCursor: number
): { text: string; nextCursor: number } {
  const s = Math.max(0, Math.min(posStart, text.length));
  const e = Math.max(s, Math.min(posEnd, text.length));
  const updated = text.slice(0, s) + insertedText + text.slice(e);
  const delta = insertedText.length - (e - s);

  let nextCursor = currentCursor;
  if (currentCursor > e) {
    nextCursor = Math.max(0, currentCursor + delta);
  } else if (currentCursor > s) {
    nextCursor = s + insertedText.length;
  }

  return { text: updated, nextCursor };
}

/**
 * Strip Markdown symbols for clean plain-text previews on dashboard cards.
 */
export function markdownToPlainText(md: string): string {
  return md
    .replace(/!\[([^\]]*)\]\([^)]+\)/g, "[Image: $1]")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/^>\s+/gm, "")
    .replace(/^[-*]\s+\[[ xX]\]\s+/gm, "• ")
    .replace(/^[-*]\s+/gm, "• ")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/\*([^*]+)\*/g, "$1")
    .replace(/~~([^~]+)~~/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\n+/g, " ")
    .trim();
}

function escapeHtml(raw: string): string {
  return raw
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function inlineMarkdownToHtml(text: string): string {
  const imagePlaceholders: string[] = [];
  const withPlaceholders = text.replace(
    /!\[([^\]]*)\]\(([^)\s]+)\)/g,
    (_match, rawAlt: string, rawSrc: string) => {
      const idx = imagePlaceholders.length;
      const resolvedSrc = resolveAttachmentUrl(rawSrc);
      imagePlaceholders.push(
        `<img src="${escapeHtml(resolvedSrc)}" alt="${escapeHtml(rawAlt)}" class="doc-inline-image" />`
      );
      return `\u0000IMG_${idx}\u0000`;
    }
  );

  const formatted = escapeHtml(withPlaceholders)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/~~([^~]+)~~/g, "<s>$1</s>");

  return formatted.replace(/\u0000IMG_(\d+)\u0000/g, (_m, idxStr: string) => {
    return imagePlaceholders[Number(idxStr)] ?? "";
  });
}

/**
 * Convert background Markdown (.md) into clean WYSIWYG HTML for the rich-text editor.
 */
export function markdownToHtml(md: string): string {
  if (!md) {
    return "<p><br></p>";
  }

  const lines = md.replace(/\r\n/g, "\n").split("\n");
  const blocks: string[] = [];
  let inUl = false;
  let inOl = false;

  const closeLists = () => {
    if (inUl) {
      blocks.push("</ul>");
      inUl = false;
    }
    if (inOl) {
      blocks.push("</ol>");
      inOl = false;
    }
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const trimmed = line.trim();

    if (trimmed === "---" || trimmed === "***") {
      closeLists();
      blocks.push("<hr>");
      continue;
    }

    if (line.startsWith("# ")) {
      closeLists();
      blocks.push(`<h1>${inlineMarkdownToHtml(line.slice(2)) || "<br>"}</h1>`);
      continue;
    }
    if (line.startsWith("## ")) {
      closeLists();
      blocks.push(`<h2>${inlineMarkdownToHtml(line.slice(3)) || "<br>"}</h2>`);
      continue;
    }
    if (line.startsWith("### ")) {
      closeLists();
      blocks.push(`<h3>${inlineMarkdownToHtml(line.slice(4)) || "<br>"}</h3>`);
      continue;
    }
    if (line.startsWith("> ")) {
      closeLists();
      blocks.push(
        `<blockquote>${inlineMarkdownToHtml(line.slice(2)) || "<br>"}</blockquote>`
      );
      continue;
    }

    // Bullet or checklist items
    if (
      line.startsWith("- [x] ") ||
      line.startsWith("- [X] ") ||
      line.startsWith("- [ ] ") ||
      line.startsWith("- ") ||
      line.startsWith("* ")
    ) {
      if (inOl) {
        blocks.push("</ol>");
        inOl = false;
      }
      if (!inUl) {
        blocks.push("<ul>");
        inUl = true;
      }
      let itemText = line.slice(2);
      if (
        line.startsWith("- [x] ") ||
        line.startsWith("- [X] ") ||
        line.startsWith("- [ ] ")
      ) {
        itemText = line.slice(6);
      }
      blocks.push(`<li>${inlineMarkdownToHtml(itemText) || "<br>"}</li>`);
      continue;
    }

    // Numbered list items
    const olMatch = line.match(/^\d+\.\s+(.*)$/);
    if (olMatch) {
      if (inUl) {
        blocks.push("</ul>");
        inUl = false;
      }
      if (!inOl) {
        blocks.push("<ol>");
        inOl = true;
      }
      blocks.push(`<li>${inlineMarkdownToHtml(olMatch[1]) || "<br>"}</li>`);
      continue;
    }

    closeLists();
    if (trimmed === "") {
      // Preserve blank lines only if consecutive or empty doc
      blocks.push("<p><br></p>");
    } else {
      blocks.push(`<p>${inlineMarkdownToHtml(line)}</p>`);
    }
  }

  closeLists();
  return blocks.join("") || "<p><br></p>";
}

/**
 * Serialize the rich-text contentEditable DOM back to Markdown in the background,
 * while simultaneously mapping the current DOM selection to Markdown character offsets!
 */
export function serializeDomWithSelection(root: HTMLElement): {
  markdown: string;
  selStart: number;
  selEnd: number;
  hasSelectionInRoot: boolean;
} {
  const sel = window.getSelection();
  let range: Range | null = null;
  if (sel && sel.rangeCount > 0 && root.contains(sel.anchorNode)) {
    range = sel.getRangeAt(0);
  }

  let md = "";
  let selStart: number | null = null;
  let selEnd: number | null = null;

  const isIgnoredElement = (el: HTMLElement): boolean => {
    const tag = el.tagName.toLowerCase();
    if (tag === "script" || tag === "style" || tag === "svg" || tag === "noscript") {
      return true;
    }
    if (el.getAttribute("aria-hidden") === "true" || el.getAttribute("contenteditable") === "false") {
      return true;
    }
    return false;
  };

  const checkBoundary = (node: Node, offset: number) => {
    if (!range) return;
    if (range.startContainer === node && range.startOffset === offset) {
      selStart = md.length;
    }
    if (range.endContainer === node && range.endOffset === offset) {
      selEnd = md.length;
    }
  };

  const serializeInline = (node: Node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      const text = node.nodeValue ?? "";
      if (range) {
        if (range.startContainer === node) {
          selStart = md.length + Math.min(range.startOffset, text.length);
        }
        if (range.endContainer === node) {
          selEnd = md.length + Math.min(range.endOffset, text.length);
        }
      }
      md += text;
      return;
    }

    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const el = node as HTMLElement;
    if (isIgnoredElement(el)) return;
    const tag = el.tagName.toLowerCase();

    if (tag === "br") {
      checkBoundary(el, 0);
      return;
    }

    if (tag === "img") {
      checkBoundary(el, 0);
      const rawSrc = el.getAttribute("src") ?? "";
      const src = rawSrc.replace(
        /^https?:\/\/[^/]+(\/api\/attachments\/\d+.*)$/,
        "$1"
      );
      const alt = (el.getAttribute("alt") ?? "image").replace(/[\[\]]/g, "");
      if (src) {
        md += `![${alt}](${src})`;
      }
      checkBoundary(el, 1);
      return;
    }

    let marker = "";
    if (tag === "strong" || tag === "b") marker = "**";
    else if (tag === "em" || tag === "i") marker = "*";
    else if (tag === "s" || tag === "strike" || tag === "del") marker = "~~";
    else if (tag === "code") marker = "`";

    md += marker;
    const children = Array.from(el.childNodes);
    for (let i = 0; i < children.length; i++) {
      checkBoundary(el, i);
      serializeInline(children[i]);
    }
    checkBoundary(el, children.length);
    md += marker;
  };

  const blocks: Node[] = [];
  const collectBlocks = (container: HTMLElement) => {
    const children = Array.from(container.childNodes);
    for (const child of children) {
      if (child.nodeType === Node.ELEMENT_NODE) {
        const el = child as HTMLElement;
        if (isIgnoredElement(el)) continue;
        const tag = el.tagName.toLowerCase();
        if (tag === "ul" || tag === "ol") {
          for (const li of Array.from(el.children)) {
            if (!isIgnoredElement(li as HTMLElement)) {
              blocks.push(li as HTMLElement);
            }
          }
        } else if (tag === "div" && el.children.length > 0 && Array.from(el.children).some((c) => ["p", "div", "h1", "h2", "h3", "blockquote", "ul", "ol"].includes(c.tagName.toLowerCase()))) {
          collectBlocks(el);
        } else {
          blocks.push(el);
        }
      } else if (child.nodeType === Node.TEXT_NODE && child.nodeValue) {
        blocks.push(child);
      }
    }
  };

  collectBlocks(root);

  let olCounter = 1;
  for (let bIdx = 0; bIdx < blocks.length; bIdx++) {
    const blockNode = blocks[bIdx];
    if (bIdx > 0) {
      md += "\n";
    }

    if (blockNode.nodeType === Node.TEXT_NODE) {
      serializeInline(blockNode);
      olCounter = 1;
      continue;
    }

    const block = blockNode as HTMLElement;
    const tag = block.tagName.toLowerCase();
    const parentTag = block.parentElement?.tagName.toLowerCase() ?? "";

    if (tag === "hr") {
      checkBoundary(block, 0);
      md += "---";
      continue;
    }

    if (tag === "img") {
      serializeInline(block);
      olCounter = 1;
      continue;
    }

    if (tag === "h1") {
      md += "# ";
      olCounter = 1;
    } else if (tag === "h2") {
      md += "## ";
      olCounter = 1;
    } else if (tag === "h3") {
      md += "### ";
      olCounter = 1;
    } else if (tag === "blockquote") {
      md += "> ";
      olCounter = 1;
    } else if (tag === "li") {
      if (parentTag === "ol") {
        md += `${olCounter}. `;
        olCounter++;
      } else {
        md += "- ";
        olCounter = 1;
      }
    } else {
      olCounter = 1;
    }

    const children = Array.from(block.childNodes);
    for (let i = 0; i < children.length; i++) {
      checkBoundary(block, i);
      serializeInline(children[i]);
    }
    checkBoundary(block, children.length);
  }

  const hasSelectionInRoot = selStart !== null;
  const finalStart = selStart ?? md.length;
  const finalEnd = selEnd ?? finalStart;
  return {
    markdown: md,
    selStart: Math.min(finalStart, finalEnd),
    selEnd: Math.max(finalStart, finalEnd),
    hasSelectionInRoot,
  };
}
