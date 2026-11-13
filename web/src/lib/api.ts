// Browser client for the backend: the API (HTTP API Gateway) and the streaming chat (Lambda Function URL).
// URLs come from NEXT_PUBLIC_API_URL / NEXT_PUBLIC_CHAT_URL, written at build time from amplify_outputs.json
// (amplify.yml) or by hand for local development (web/.env.local). The anonymous session token lives in
// localStorage: it is this browser's identity, so clearing browser data loses access to its documents.
import type { Chunk, ChatEvent, Conversation, Doc, Insights, Message, OutlineItem } from "./types";

const API = (process.env.NEXT_PUBLIC_API_URL ?? "").replace(/\/$/, "");
const CHAT = (process.env.NEXT_PUBLIC_CHAT_URL ?? "").replace(/\/$/, "");
const TOKEN_KEY = "billing-rag.session";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

let pending: Promise<string> | null = null;

async function token(): Promise<string> {
  const saved = localStorage.getItem(TOKEN_KEY);
  if (saved) return saved;
  pending ??= fetch(`${API}/session`, { method: "POST" })
    .then(async (r) => {
      if (!r.ok) throw new ApiError(r.status, "Could not start a session");
      const t = (await r.json()).token as string;
      localStorage.setItem(TOKEN_KEY, t);
      return t;
    })
    .finally(() => (pending = null));
  return pending;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const send = async () =>
    fetch(`${API}${path}`, {
      method,
      headers: { authorization: `Bearer ${await token()}`, ...(body ? { "content-type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
  let r = await send();
  if (r.status === 401) {
    // The token is no longer valid (for example the signing key was rotated): start a new identity once.
    localStorage.removeItem(TOKEN_KEY);
    r = await send();
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new ApiError(r.status, data.error ?? `Request failed (${r.status})`);
  return data as T;
}

export const api = {
  documents: () => request<{ documents: Doc[] }>("GET", "/documents").then((d) => d.documents),
  document: (id: string) => request<Doc>("GET", `/documents/${id}`),
  insights: (id: string) => request<Insights>("GET", `/documents/${id}/insights`),
  outline: (id: string) => request<{ items: OutlineItem[] }>("GET", `/documents/${id}/outline`).then((d) => d.items),
  chunk: (id: string, chunkId: string) => request<Chunk>("GET", `/documents/${id}/chunks/${chunkId}`),
  pdfUrl: (id: string) => request<{ url: string }>("GET", `/documents/${id}/pdf`).then((d) => d.url),
  conversations: (id: string) =>
    request<{ conversations: Conversation[] }>("GET", `/documents/${id}/conversations`).then((d) => d.conversations),
  messages: (conversationId: string) =>
    request<{ messages: Message[] }>("GET", `/conversations/${conversationId}`).then((d) => d.messages),
  feedback: (messageId: string, value: 1 | -1) => request("POST", `/messages/${messageId}/feedback`, { value }),
  remove: (id: string) => request("DELETE", `/documents/${id}`),

  /** Ask for an upload slot, then send the file straight to S3 (a presigned POST: S3 enforces size and type). */
  async upload(file: File): Promise<string> {
    const slot = await request<{ document_id: string; upload: { url: string; fields: Record<string, string> } }>(
      "POST", "/documents/upload-url", { filename: file.name, size: file.size });
    const form = new FormData();
    Object.entries(slot.upload.fields).forEach(([k, v]) => form.append(k, v));
    form.append("file", file);
    const r = await fetch(slot.upload.url, { method: "POST", body: form });
    if (!r.ok) throw new ApiError(r.status, "The upload was rejected (PDF files up to 50 MB only).");
    return slot.document_id;
  },

  /** Stream an answer: one JSON event per line (conversation, token..., done | error). */
  async *chat(id: string, message: string, conversationId: string | null, signal?: AbortSignal):
    AsyncGenerator<ChatEvent> {
    const r = await fetch(`${CHAT}/documents/${id}/chat`, {
      method: "POST",
      headers: { authorization: `Bearer ${await token()}`, "content-type": "application/json" },
      body: JSON.stringify({ message, conversation_id: conversationId }),
      signal,
    });
    if (!r.ok || !r.body) {
      const data = await r.json().catch(() => ({}));
      throw new ApiError(r.status, data.error ?? `Request failed (${r.status})`);
    }
    const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += value;
      let nl: number;
      while ((nl = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, nl).trim();
        buffer = buffer.slice(nl + 1);
        if (line) yield JSON.parse(line) as ChatEvent;
      }
    }
    if (buffer.trim()) yield JSON.parse(buffer) as ChatEvent;
  },
};

export const IN_PROGRESS = new Set(["uploaded", "parsing", "chunking", "tagging", "embedding", "ready_for_insights",
  "extracting"]);
