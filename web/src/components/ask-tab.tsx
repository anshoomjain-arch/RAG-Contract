"use client";

import { Copy, History, Plus, SendHorizontal, Square, ThumbsDown, ThumbsUp } from "lucide-react";
import { Fragment, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { CitationChip } from "@/components/citations";
import { CheckFlag } from "@/components/status";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";
import type { Check, Citation, Conversation } from "@/lib/types";

const SUGGESTIONS = [
  "What are the termination rights?",
  "What are the monthly charges per site?",
  "What happens if an invoice is paid late?",
  "What are the liability caps?",
  "Which documents prevail if they conflict?",
];
const BRACKET = /\[([^[\]]{3,200})\]/g;

type Turn = {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations: Citation[];
  check?: Check;
  unknown?: string[];
  feedback?: number | null;
  streaming?: boolean;
};

const norm = (s: string) => s.toLowerCase().replace(/["'“”‘’]/g, "").replace(/pdf p\./g, "p.")
  .replace(/\s+/g, " ").trim().replace(/^[ ,.]+|[ ,.]+$/g, "");
const noPage = (s: string) => s.replace(/,?\s*\b(?:pdf\s+)?(?:p|pp|page)\.?\s*\d+(?:\s*-\s*\d+)?/gi, "");

/** The citation a bracketed reference in the answer points to (same matching rules as the backend). */
function resolve(part: string, citations: Citation[]): Citation | undefined {
  const key = norm(part);
  const exact = citations.find((c) => c.cite && norm(c.cite) === key);
  if (exact) return exact;
  let label = norm(noPage(part));
  while (label) {
    const hit = citations.find((c) => norm(c.label) === label);
    if (hit) return hit;
    const shorter = label.replace(/(?:\.\d+|\s*\([^)]*\))$/, "");
    if (shorter === label) return undefined;
    label = shorter;
  }
}

/** Answer text with "[Clause 8.2, p. 33]" turned into clickable citation chips. */
function AnswerText({ text, citations }: { text: string; citations: Citation[] }) {
  const out: React.ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(BRACKET)) {
    out.push(text.slice(last, m.index));
    const parts = m[1].split(";").map((p) => p.trim()).filter(Boolean);
    const resolved = parts.map((p) => (p.split(" ").length <= 12 ? resolve(p, citations) : undefined));
    if (resolved.some(Boolean)) {
      out.push(<span key={m.index} className="inline-flex flex-wrap gap-1 align-baseline">
        {parts.map((p, i) => resolved[i]
          ? <CitationChip key={i} citation={{ ...resolved[i]!, cite: p }} />
          : <span key={i} className="text-xs text-muted-foreground">[{p}]</span>)}
      </span>);
    } else {
      out.push(m[0]);
    }
    last = m.index! + m[0].length;
  }
  out.push(text.slice(last));
  return <p className="leading-relaxed whitespace-pre-wrap">{out.map((n, i) => <Fragment key={i}>{n}</Fragment>)}</p>;
}

function AssistantTurn({ turn, onFeedback }: { turn: Turn; onFeedback: (v: 1 | -1) => void }) {
  return (
    <div className="flex flex-col gap-2">
      {turn.streaming
        ? <p className="leading-relaxed whitespace-pre-wrap">{turn.content}<span className="ml-0.5 inline-block h-4
            w-1.5 animate-pulse bg-foreground/60 align-text-bottom" aria-hidden /></p>
        : <AnswerText text={turn.content} citations={turn.citations} />}
      {!turn.streaming && !turn.id.startsWith("a-") && (
        <div className="flex flex-wrap items-center gap-1">
          <CheckFlag check={turn.check ?? null} />
          {turn.unknown && turn.unknown.length > 0 && (
            <span className="text-[11px] text-muted-foreground">
              Unmatched reference{turn.unknown.length > 1 ? "s" : ""}: {turn.unknown.join("; ")}
            </span>
          )}
          <span className="ml-auto flex gap-0.5">
            <Button variant="ghost" size="icon-xs" aria-label="Copy answer"
                    onClick={() => navigator.clipboard.writeText(turn.content).then(() => toast.success("Copied."))}>
              <Copy />
            </Button>
            <Button variant="ghost" size="icon-xs" aria-label="Helpful" aria-pressed={turn.feedback === 1}
                    className={turn.feedback === 1 ? "text-foreground" : "text-muted-foreground"}
                    onClick={() => onFeedback(1)}><ThumbsUp /></Button>
            <Button variant="ghost" size="icon-xs" aria-label="Not helpful" aria-pressed={turn.feedback === -1}
                    className={turn.feedback === -1 ? "text-foreground" : "text-muted-foreground"}
                    onClick={() => onFeedback(-1)}><ThumbsDown /></Button>
          </span>
        </div>
      )}
    </div>
  );
}

export function AskTab({ documentId }: { documentId: string }) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [past, setPast] = useState<Conversation[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const abort = useRef<AbortController | null>(null);
  const end = useRef<HTMLDivElement>(null);
  const seq = useRef(0);                        // local IDs for turns until the server returns a message ID

  useEffect(() => { api.conversations(documentId).then(setPast).catch(() => {}); }, [documentId, conversationId]);
  useEffect(() => { end.current?.scrollIntoView({ block: "nearest" }); }, [turns]);

  function update(id: string, patch: Partial<Turn>) {
    setTurns((ts) => ts.map((t) => (t.id === id ? { ...t, ...patch } : t)));
  }

  async function ask(question: string) {
    const q = question.trim();
    if (!q || busy) return;
    setInput("");
    setBusy(true);
    seq.current += 1;
    const pendingId = `a-${seq.current}`;
    setTurns((ts) => [...ts, { id: `u-${seq.current}`, role: "user", content: q, citations: [] },
                      { id: pendingId, role: "assistant", content: "", citations: [], streaming: true }]);
    abort.current = new AbortController();
    let text = "";
    try {
      for await (const ev of api.chat(documentId, q, conversationId, abort.current.signal)) {
        if (ev.type === "conversation") setConversationId(ev.conversation_id);
        else if (ev.type === "token") { text += ev.text; update(pendingId, { content: text }); }
        else if (ev.type === "done") {
          update(pendingId, { id: ev.message_id, content: ev.answer, citations: ev.citations, check: ev.check,
                              unknown: ev.unknown_citations, streaming: false, feedback: null });
        } else if (ev.type === "error") throw new Error(ev.error);
      }
    } catch (e) {
      const aborted = (e as Error).name === "AbortError";
      update(pendingId, { content: text || (aborted ? "Stopped." : `⚠ ${(e as Error).message}`), streaming: false });
      if (!aborted) toast.error((e as Error).message);
    } finally {
      setBusy(false);
      abort.current = null;
    }
  }

  async function feedback(turn: Turn, value: 1 | -1) {
    try {
      await api.feedback(turn.id, value);
      update(turn.id, { feedback: value });
    } catch (e) {
      toast.error((e as Error).message);
    }
  }

  async function openConversation(c: Conversation) {
    try {
      const msgs = await api.messages(c.conversation_id);
      setTurns(msgs.map((m) => ({ id: m.message_id, role: m.role, content: m.content, citations: m.citations ?? [],
                                  check: m.check, unknown: m.unknown_citations ?? [], feedback: m.feedback })));
      setConversationId(c.conversation_id);
    } catch (e) {
      toast.error((e as Error).message);
    }
  }

  return (
    <div className="grid gap-6 lg:grid-cols-[1fr_15rem]">
      <div className="flex min-h-[60vh] flex-col gap-4">
        <div className="flex flex-1 flex-col gap-5" aria-live="polite" aria-busy={busy}>
          {turns.length === 0 && (
            <div className="flex flex-col gap-3">
              <p className="text-sm text-muted-foreground">
                Ask about this contract. Answers come only from its clauses and cite where they come from.
              </p>
              <div className="flex flex-wrap gap-2">
                {SUGGESTIONS.map((s) => (
                  <Button key={s} variant="outline" size="sm" onClick={() => ask(s)}>{s}</Button>
                ))}
              </div>
            </div>
          )}
          {turns.map((t) => t.role === "user" ? (
            <div key={t.id} className="ml-auto max-w-[85%] rounded-2xl rounded-br-sm bg-primary px-4 py-2
                                       text-primary-foreground">
              <span className="sr-only">You asked: </span>{t.content}
            </div>
          ) : (
            <div key={t.id} className="max-w-full">
              <span className="sr-only">Answer: </span>
              <AssistantTurn turn={t} onFeedback={(v) => feedback(t, v)} />
            </div>
          ))}
          <div ref={end} />
        </div>
        <form className="sticky bottom-0 flex items-end gap-2 bg-background py-2"
              onSubmit={(e) => { e.preventDefault(); ask(input); }}>
          <Textarea value={input} onChange={(e) => setInput(e.target.value)} rows={2} maxLength={2000}
                    placeholder="Ask a question about this contract" aria-label="Your question"
                    onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(input); } }}
                    className="min-h-12 resize-none" />
          {busy
            ? <Button type="button" variant="outline" size="icon-lg" onClick={() => abort.current?.abort()}
                      aria-label="Stop"><Square /></Button>
            : <Button type="submit" size="icon-lg" disabled={!input.trim()} aria-label="Send"><SendHorizontal />
              </Button>}
        </form>
      </div>
      <aside className="flex flex-col gap-2" aria-label="Conversations">
        <Button variant="outline" onClick={() => { setTurns([]); setConversationId(null); }} disabled={busy}>
          <Plus /> New conversation
        </Button>
        {past.length > 0 && (
          <>
            <p className="mt-2 flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
              <History className="size-3.5" aria-hidden /> Earlier conversations
            </p>
            <ul className="flex flex-col gap-1">
              {past.map((c) => (
                <li key={c.conversation_id}>
                  <button type="button" onClick={() => openConversation(c)} disabled={busy}
                          aria-current={c.conversation_id === conversationId ? "true" : undefined}
                          className="w-full truncate rounded-md px-2 py-1.5 text-left text-sm hover:bg-muted
                                     focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none
                                     aria-[current=true]:bg-muted">
                    {c.first_question ?? "(empty)"}
                  </button>
                </li>
              ))}
            </ul>
          </>
        )}
      </aside>
    </div>
  );
}
