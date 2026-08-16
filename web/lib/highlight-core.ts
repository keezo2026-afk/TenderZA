/**
 * Pure parsing logic behind search highlighting (Blueprint §12).
 *
 * Split out of highlight.tsx so it can be unit-tested with the Node test
 * runner: no JSX, no React import, no DOM. highlight.tsx turns the segments
 * this module produces into elements.
 */

/** Matches the delimiters emitted by src/tenderza/search/highlight.py. */
const MARK_RE = /<mark>([\s\S]*?)<\/mark>/g;

/** Entities the backend introduces when it escapes text pre-highlight. */
const ENTITIES: Record<string, string> = {
  "&amp;": "&",
  "&lt;": "<",
  "&gt;": ">",
  "&quot;": '"',
  "&#39;": "'",
};

/**
 * Reverse the backend's pre-highlight escaping so React can re-escape.
 *
 * Without this, a title containing "R&D" renders as "R&amp;D": the text was
 * escaped once for ts_headline, and React would escape the ampersand again.
 */
export function decodeEntities(text: string): string {
  return text.replace(/&(amp|lt|gt|quot|#39);/g, (m) => ENTITIES[m] ?? m);
}

export interface Segment {
  /** True when this run of text matched the query and should be marked. */
  marked: boolean;
  text: string;
}

/**
 * Split a highlight string into marked/unmarked segments.
 *
 * Every returned `text` is fully decoded plain text: callers must render it
 * as text (React escapes it), never as HTML.
 */
export function segments(text: string | null | undefined): Segment[] {
  if (!text) return [];

  const out: Segment[] = [];
  let last = 0;

  MARK_RE.lastIndex = 0;
  for (let m = MARK_RE.exec(text); m !== null; m = MARK_RE.exec(text)) {
    if (m.index > last) {
      out.push({ marked: false, text: decodeEntities(text.slice(last, m.index)) });
    }
    // m[1] cannot contain nested markup: the backend escaped '<' before
    // highlighting, so the only '<' left in the string are our own tags.
    out.push({ marked: true, text: decodeEntities(m[1]) });
    last = m.index + m[0].length;
  }

  if (last < text.length) {
    out.push({ marked: false, text: decodeEntities(text.slice(last)) });
  }
  return out;
}

/** Strip highlighting entirely — for `title`/`aria-label` attributes. */
export function plainText(text: string | null | undefined): string {
  if (!text) return "";
  return decodeEntities(text.replace(/<\/?mark>/g, ""));
}

/** True when a highlight string actually marked something. */
export function hasMatch(text: string | null | undefined): boolean {
  return !!text && text.includes("<mark>");
}
