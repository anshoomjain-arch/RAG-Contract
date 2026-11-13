"use client";

import Link from "next/link";
import { FileUp, Info, Trash2 } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { StatusChip } from "@/components/status";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { api, IN_PROGRESS } from "@/lib/api";
import type { Doc } from "@/lib/types";

const MAX_MB = 50;

function UploadZone({ onUploaded }: { onUploaded: (id: string) => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState(false);

  async function send(file: File | undefined) {
    if (!file) return;
    if (file.type !== "application/pdf" && !file.name.toLowerCase().endsWith(".pdf")) {
      toast.error("Only PDF files can be uploaded.");
      return;
    }
    if (file.size > MAX_MB * 1024 * 1024) {
      toast.error(`The file is larger than ${MAX_MB} MB.`);
      return;
    }
    setBusy(true);
    try {
      onUploaded(await api.upload(file));
      toast.success("Uploaded. Processing has started.");
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
      if (input.current) input.current.value = "";
    }
  }

  return (
    <div
      onDragOver={(e) => { e.preventDefault(); setOver(true); }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => { e.preventDefault(); setOver(false); send(e.dataTransfer.files[0]); }}
      className={`flex flex-col items-center justify-center gap-3 rounded-xl border-2 border-dashed px-6 py-10
                  text-center transition-colors ${over ? "border-primary bg-muted" : "border-border"}`}>
      <FileUp className="size-8 text-muted-foreground" aria-hidden />
      <div>
        <p className="font-medium">Drop a contract PDF here</p>
        <p className="text-sm text-muted-foreground">Up to {MAX_MB} MB and 500 pages. Text-based PDFs only.</p>
      </div>
      <input ref={input} type="file" accept="application/pdf,.pdf" className="sr-only" id="pdf-input"
             onChange={(e) => send(e.target.files?.[0])} />
      <Button onClick={() => input.current?.click()} disabled={busy} aria-controls="pdf-input">
        {busy ? "Uploading..." : "Choose a file"}
      </Button>
    </div>
  );
}

export default function Library() {
  const [docs, setDocs] = useState<Doc[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [tick, setTick] = useState(0);          // bumped to reload: after an upload or delete, and while polling
  const load = useCallback(() => setTick((n) => n + 1), []);

  useEffect(() => {
    let live = true;
    api.documents()
      .then((d) => { if (live) { setDocs(d); setError(null); } })
      .catch((e) => live && setError((e as Error).message));
    return () => { live = false; };
  }, [tick]);

  const busy = docs?.some((d) => IN_PROGRESS.has(d.status));
  useEffect(() => {
    if (!busy) return;
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [busy, load]);

  async function remove(doc: Doc) {
    if (!confirm(`Delete "${doc.title}" and its summary and chats? This cannot be undone.`)) return;
    try {
      await api.remove(doc.id);
      toast.success("Deleted.");
      load();
    } catch (e) {
      toast.error((e as Error).message);
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-3xl font-semibold">Documents</h1>
        <p className="mt-1 flex items-center gap-1.5 text-sm text-muted-foreground">
          <Info className="size-4 shrink-0" aria-hidden />
          Your documents are tied to this browser. Clearing browser data will remove access to them.
        </p>
      </div>

      <UploadZone onUploaded={() => load()} />

      <section aria-labelledby="doc-list" className="flex flex-col gap-3">
        <h2 id="doc-list" className="text-lg font-semibold">Your documents</h2>
        {error && <p className="text-sm text-destructive">{error}</p>}
        {docs === null && !error && [0, 1].map((i) => <Skeleton key={i} className="h-16 w-full" />)}
        {docs?.length === 0 && <p className="text-sm text-muted-foreground">No documents yet.</p>}
        <ul className="flex flex-col gap-2">
          {docs?.map((d) => (
            <li key={d.id}>
              <Card className="py-0">
                <CardContent className="flex items-center gap-3 py-3">
                  <Link href={`/document?id=${d.id}`}
                        className="min-w-0 flex-1 rounded-md focus-visible:ring-2 focus-visible:ring-ring
                                   focus-visible:outline-none">
                    <p className="truncate font-medium">{d.title}</p>
                    <p className="truncate text-xs text-muted-foreground">
                      {new Date(d.created_at).toLocaleString()}
                      {d.page_count ? ` · ${d.page_count} pages` : ""}
                      {d.status === "failed" && d.error ? ` · ${d.error}` : ""}
                    </p>
                  </Link>
                  <StatusChip status={d.status} />
                  <Button variant="ghost" size="icon-sm" onClick={() => remove(d)} aria-label={`Delete ${d.title}`}>
                    <Trash2 />
                  </Button>
                </CardContent>
              </Card>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
