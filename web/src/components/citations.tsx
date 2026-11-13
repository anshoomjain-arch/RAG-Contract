"use client";

import { createContext, useContext } from "react";

import type { Citation } from "@/lib/types";

/** Opens the source panel for a citation; provided by the document workspace. */
export type OpenSource = (citation: Citation, quote?: string) => void;
export const SourceContext = createContext<OpenSource>(() => {});

export function citeText(c: Citation): string {
  if (c.cite) return c.cite;
  const page = c.page_printed ? `p. ${c.page_printed}` : c.page_pdf ? `PDF p. ${c.page_pdf}` : "";
  return [c.label, page].filter(Boolean).join(", ");
}

export function CitationChip({ citation, quote }: { citation: Citation; quote?: string }) {
  const open = useContext(SourceContext);
  return (
    <button type="button" onClick={() => open(citation, quote)}
            className="inline-flex max-w-full items-center rounded-md border bg-muted/50 px-1.5 py-0.5 align-baseline
                       text-[11px] font-medium text-muted-foreground transition-colors hover:bg-muted
                       hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
            aria-label={`Open source: ${citeText(citation)}`}>
      <span className="truncate">{citeText(citation)}</span>
    </button>
  );
}

export function Citations({ citations, quotes }: { citations: Citation[]; quotes?: string[] }) {
  if (!citations.length) return null;
  return (
    <span className="inline-flex flex-wrap gap-1">
      {citations.map((c, i) => <CitationChip key={`${c.chunk_id}-${i}`} citation={c} quote={quotes?.[0]} />)}
    </span>
  );
}
