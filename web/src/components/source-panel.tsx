"use client";

import dynamic from "next/dynamic";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { useEffect, useState } from "react";

import { citeText } from "@/components/citations";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import type { Chunk, Citation } from "@/lib/types";

const PdfView = dynamic(() => import("@/components/pdf-view"), {
  ssr: false, loading: () => <Skeleton className="aspect-[3/4] w-full" />,
});

/** seq changes on every open, so the panel body starts fresh even for the same citation. */
export type SourceRequest = { citation: Citation; quote?: string; seq: number };
const URL_TTL_MS = 4 * 60 * 1000;    // presigned links last 5 minutes

/** Clause text with the cited quote marked, when the quote appears in it. */
function ClauseText({ text, quote }: { text: string; quote?: string }) {
  const at = quote ? text.toLowerCase().indexOf(quote.toLowerCase().slice(0, 120)) : -1;
  if (!quote || at < 0) return <p className="text-sm leading-relaxed whitespace-pre-wrap">{text}</p>;
  const end = at + quote.length;
  return (
    <p className="text-sm leading-relaxed whitespace-pre-wrap">
      {text.slice(0, at)}
      <mark className="rounded-sm bg-amber-200/70 px-0.5 text-foreground dark:bg-amber-400/30">
        {text.slice(at, end)}</mark>
      {text.slice(end)}
    </p>
  );
}

function SourceBody({ documentId, request, pdfUrl }: {
  documentId: string; request: SourceRequest; pdfUrl: string | null;
}) {
  const c = request.citation;
  const [chunk, setChunk] = useState<Chunk | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(c.page_pdf ?? 1);
  const [pages, setPages] = useState<number | null>(null);

  useEffect(() => {
    api.chunk(documentId, c.chunk_id).then(setChunk).catch((e) => setError((e as Error).message));
  }, [documentId, c.chunk_id]);

  const first = chunk?.page_pdf ?? c.page_pdf ?? 1;
  const last = Math.max(chunk?.page_end_pdf ?? c.page_end_pdf ?? first, first);
  const printed = chunk?.page_printed && chunk.page_pdf ? page - chunk.page_pdf + chunk.page_printed : null;
  const cited = page >= first && page <= last;

  return (
    <>
      <SheetHeader className="border-b">
        <SheetTitle>{citeText(c)}</SheetTitle>
        <SheetDescription>{chunk?.breadcrumb ?? "Source clause and page"}</SheetDescription>
      </SheetHeader>
      <div className="flex flex-col gap-4 p-4">
        {error && <p className="text-sm text-destructive">{error}</p>}
        {!chunk && !error && <Skeleton className="h-32 w-full" />}
        {chunk && (
          <section aria-label="Clause text" className="max-h-72 overflow-y-auto rounded-md border bg-muted/30 p-3">
            <ClauseText text={chunk.text} quote={request.quote} />
          </section>
        )}
        <section aria-label="PDF page" className="flex flex-col gap-2">
          <div className="flex items-center justify-between gap-2">
            <p className="text-sm text-muted-foreground" aria-live="polite">
              PDF page {page}{pages ? ` of ${pages}` : ""}{printed ? ` (printed p. ${printed})` : ""}
              {cited ? " · cited" : ""}
            </p>
            <div className="flex gap-1">
              <Button variant="outline" size="icon-sm" onClick={() => setPage(Math.max(1, page - 1))}
                      disabled={page <= 1} aria-label="Previous page"><ChevronLeft /></Button>
              <Button variant="outline" size="icon-sm" onClick={() => setPage(page + 1)}
                      disabled={!!pages && page >= pages} aria-label="Next page"><ChevronRight /></Button>
            </div>
          </div>
          {pdfUrl
            ? <PdfView url={pdfUrl} page={page} onPages={setPages} highlight={cited ? chunk?.text ?? "" : ""} />
            : <Skeleton className="aspect-[3/4] w-full" />}
        </section>
      </div>
    </>
  );
}

export function SourcePanel({ documentId, request, onClose }: {
  documentId: string; request: SourceRequest | null; onClose: () => void;
}) {
  const [pdf, setPdf] = useState<{ url: string; at: number } | null>(null);

  useEffect(() => {
    if (!request || (pdf && Date.now() - pdf.at < URL_TTL_MS)) return;
    api.pdfUrl(documentId).then((url) => setPdf({ url, at: Date.now() })).catch(() => setPdf({ url: "", at: Date.now() }));    // no retry loop; reopen after the TTL
  }, [request, documentId, pdf]);

  return (
    <Sheet open={!!request} onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent side="right" className="w-full gap-0 overflow-y-auto data-[side=right]:sm:max-w-2xl">
        {request && <SourceBody key={request.seq} documentId={documentId} request={request}
                                pdfUrl={pdf?.url || null} />}
      </SheetContent>
    </Sheet>
  );
}
