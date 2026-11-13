"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { AlertTriangle, ArrowLeft, Check, Circle, Loader2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { AskTab } from "@/components/ask-tab";
import { SourceContext } from "@/components/citations";
import { ClausesTab } from "@/components/clauses-tab";
import { KeyInfoTab, SummaryTab } from "@/components/insights-tabs";
import { SourcePanel, type SourceRequest } from "@/components/source-panel";
import { STEPS, StatusChip, stepOf } from "@/components/status";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api, IN_PROGRESS } from "@/lib/api";
import type { Doc, Insights } from "@/lib/types";

function ProcessingView({ doc }: { doc: Doc }) {
  const current = stepOf(doc.status);
  return (
    <div className="flex flex-col gap-6">
      <ol className="flex flex-col gap-3 sm:flex-row sm:gap-0" aria-label="Processing steps">
        {STEPS.map((step, i) => {
          const state = i < current ? "done" : i === current ? "active" : "todo";
          return (
            <li key={step} className="flex flex-1 items-center gap-2 sm:flex-col sm:gap-1.5 sm:text-center"
                aria-current={state === "active" ? "step" : undefined}>
              <span className={`flex size-7 items-center justify-center rounded-full border text-xs
                ${state === "done" ? "border-primary bg-primary text-primary-foreground" : ""}
                ${state === "active" ? "border-primary text-foreground" : ""}
                ${state === "todo" ? "text-muted-foreground" : ""}`}>
                {state === "done" ? <Check className="size-4" aria-hidden />
                  : state === "active" ? <Loader2 className="size-4 animate-spin" aria-hidden />
                  : <Circle className="size-2" aria-hidden />}
              </span>
              <span className={`text-sm ${state === "todo" ? "text-muted-foreground" : "font-medium"}`}>
                {step}
                <span className="sr-only"> ({state === "done" ? "complete" : state === "active" ? "in progress"
                  : "not started"})</span>
              </span>
            </li>
          );
        })}
      </ol>
      <div className="flex flex-col gap-1.5">
        <Progress value={doc.progress} aria-label={`${STEPS[current]}: ${doc.progress}%`} />
        <p className="text-sm text-muted-foreground" aria-live="polite">
          {STEPS[current]}… {doc.progress}%. Large contracts take a few minutes; you can leave this page.
        </p>
      </div>
      <ParseWarnings doc={doc} />
    </div>
  );
}

function ParseWarnings({ doc }: { doc: Doc }) {
  const warnings = doc.parse_warnings ?? [];
  if (!warnings.length) return null;
  return (
    <Alert>
      <AlertTriangle aria-hidden />
      <AlertTitle>{warnings.length} parse warning{warnings.length > 1 ? "s" : ""}</AlertTitle>
      <AlertDescription>
        <ul className="list-disc pl-4">
          {warnings.slice(0, 6).map((w, i) => <li key={i}>{w.message}</li>)}
        </ul>
      </AlertDescription>
    </Alert>
  );
}

export function Workspace() {
  const id = useSearchParams().get("id") ?? "";
  const [doc, setDoc] = useState<Doc | null>(null);
  const [insights, setInsights] = useState<Insights | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [source, setSource] = useState<SourceRequest | null>(null);

  const [tick, setTick] = useState(0);          // bumped by the poll timer to reload the status

  useEffect(() => {
    if (!id) return;
    let live = true;
    api.document(id)
      .then(async (d) => {
        if (!live) return;
        setDoc(d);
        if (d.status === "ready") {
          const ins = await api.insights(id);
          if (live) setInsights(ins);
        }
      })
      .catch((e) => live && setError((e as Error).message));
    return () => { live = false; };
  }, [id, tick]);

  const working = doc ? IN_PROGRESS.has(doc.status) : false;
  useEffect(() => {
    if (!working) return;
    const t = setInterval(() => setTick((n) => n + 1), 4000);
    return () => clearInterval(t);
  }, [working]);

  const openSource = useCallback((citation: SourceRequest["citation"], quote?: string) =>
    setSource((prev) => ({ citation, quote, seq: (prev?.seq ?? 0) + 1 })), []);

  if (!id || error) {
    return (
      <div className="flex flex-col gap-3">
        <BackLink />
        <p className="text-destructive">{error ?? "No document selected."}</p>
      </div>
    );
  }
  if (!doc) return <Skeleton className="h-64 w-full" />;

  return (
    <SourceContext.Provider value={openSource}>
      <div className="flex flex-col gap-5">
        <div className="flex flex-col gap-2">
          <BackLink />
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="min-w-0 flex-1 text-2xl font-semibold sm:text-3xl">{doc.title}</h1>
            <StatusChip status={doc.status} />
          </div>
          {doc.page_count && <p className="text-sm text-muted-foreground">{doc.page_count} pages</p>}
        </div>

        {doc.status === "failed" && (
          <Alert variant="destructive">
            <AlertTriangle aria-hidden />
            <AlertTitle>Processing failed</AlertTitle>
            <AlertDescription>{doc.error ?? "Unknown error."}</AlertDescription>
          </Alert>
        )}
        {working && <ProcessingView doc={doc} />}

        {doc.status === "ready" && (
          <Tabs defaultValue="summary" className="gap-4">
            <TabsList className="w-full sm:w-fit">
              <TabsTrigger value="summary">Summary</TabsTrigger>
              <TabsTrigger value="key-info">Key information</TabsTrigger>
              <TabsTrigger value="clauses">Clauses</TabsTrigger>
              <TabsTrigger value="ask">Ask</TabsTrigger>
            </TabsList>
            <TabsContent value="summary">
              {insights ? <SummaryTab insights={insights} /> : <Skeleton className="h-64 w-full" />}
              <div className="mt-6"><ParseWarnings doc={doc} /></div>
            </TabsContent>
            <TabsContent value="key-info">
              {insights ? <KeyInfoTab insights={insights} /> : <Skeleton className="h-64 w-full" />}
            </TabsContent>
            <TabsContent value="clauses"><ClausesTab documentId={id} /></TabsContent>
            <TabsContent value="ask" keepMounted><AskTab documentId={id} /></TabsContent>
          </Tabs>
        )}
      </div>
      <SourcePanel documentId={id} request={source} onClose={() => setSource(null)} />
    </SourceContext.Provider>
  );
}

function BackLink() {
  return (
    <Link href="/" className="inline-flex w-fit items-center gap-1 rounded-md text-sm text-muted-foreground
                              hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring
                              focus-visible:outline-none">
      <ArrowLeft className="size-4" aria-hidden /> Documents
    </Link>
  );
}
