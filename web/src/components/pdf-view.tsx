"use client";

// Loaded with next/dynamic and ssr: false (react-pdf needs the browser). The worker is set in this module, as
// react-pdf requires.
import "react-pdf/dist/Page/TextLayer.css";
import "react-pdf/dist/Page/AnnotationLayer.css";

import { useCallback, useEffect, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";

pdfjs.GlobalWorkerOptions.workerSrc = new URL("pdfjs-dist/build/pdf.worker.min.mjs", import.meta.url).toString();

const norm = (s: string) => s.toLowerCase().replace(/[“”"']/g, "").replace(/\s+/g, " ").trim();
const escape = (s: string) => s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]!);

/** One PDF page, with the text lines that belong to `highlight` (the cited clause) marked. */
export default function PdfView({ url, page, highlight, onPages }: {
  url: string; page: number; highlight: string; onPages?: (n: number) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(600);
  useEffect(() => {
    if (!box.current) return;
    const ro = new ResizeObserver(([e]) => setWidth(Math.floor(e.contentRect.width)));
    ro.observe(box.current);
    return () => ro.disconnect();
  }, []);

  const target = norm(highlight);
  const render = useCallback(({ str }: { str: string }) => {
    const s = norm(str);
    // whole lines or long fragments only: a short word like "provide" also appears in unrelated clauses
    const hit = (s.length >= 12 || s.split(" ").length >= 3) && target.includes(s);
    return hit ? `<mark class="pdf-hit">${escape(str)}</mark>` : escape(str);
  }, [target]);

  return (
    <div ref={box} className="w-full overflow-hidden rounded-md border bg-white">
      <Document file={url} onLoadSuccess={(pdf) => onPages?.(pdf.numPages)}
                loading={<p className="p-4 text-sm text-neutral-500">Loading PDF…</p>}
                error={<p className="p-4 text-sm text-red-700">The PDF could not be loaded.</p>}>
        <Page pageNumber={page} width={width} customTextRenderer={render} renderAnnotationLayer={false}
              loading={<p className="p-4 text-sm text-neutral-500">Loading page {page}…</p>} />
      </Document>
    </div>
  );
}
