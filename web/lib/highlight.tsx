/**
 * Rendering for server-side keyword highlighting (Blueprint §12).
 *
 * The API returns strings where matched terms are wrapped in `<mark>` and all
 * other markup has been HTML-escaped before `ts_headline` ever saw it (see
 * src/tenderza/search/highlight.py).
 *
 * We still do NOT use `dangerouslySetInnerHTML` here. The highlight string is
 * built from tender text that originates on third-party government websites,
 * so it is attacker-influenced data; making the UI's safety depend on a
 * `replace()` chain in the database layer means one regression there becomes
 * stored XSS here. Instead we parse out our own `<mark>` delimiters and let
 * React escape every remaining character. Escaping is then enforced by React,
 * not by convention, and both layers have to fail together to hurt us.
 *
 * The parsing lives in ./highlight-core.ts so it can be unit-tested.
 */
import type { ReactNode } from "react";

import { segments } from "./highlight-core";

export { decodeEntities, hasMatch, plainText } from "./highlight-core";

/**
 * Turn a highlight string into React nodes, `<mark>` elements included.
 *
 * Returns `null` when there is nothing to render so callers can fall back to
 * the plain field.
 */
export function renderHighlight(
  text: string | null | undefined,
  key = "hl",
): ReactNode {
  const parts = segments(text);
  if (parts.length === 0) return null;

  return parts.map((part, i) =>
    part.marked ? (
      <mark key={`${key}-${i}`} className="tz-mark">
        {part.text}
      </mark>
    ) : (
      // Plain strings need no key wrapper, but the array form does.
      <span key={`${key}-${i}`}>{part.text}</span>
    ),
  );
}
