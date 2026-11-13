// Shapes returned by the API (services/api/queries.py) and the insights generator (services/insights/generate.py).

export type Check = { status: "verified" | "unverified" | "no_values"; checked: string[]; missing: string[] } | null;

export type Citation = {
  chunk_id: string;
  clause_id: string | null;
  label: string;
  page_pdf: number | null;
  page_end_pdf: number | null;
  page_printed: number | null;
  cite?: string;
};

export type Item = { value: string; citations: Citation[]; check: Check; quotes?: string[] };
export type Field = { key: string; label: string; items: Item[] | null };
export type Section = { key: string; title: string; fields: Field[] };

export type Insights = {
  key_info: { sections: Section[]; observations: Item[]; missing: Item[] };
  summary: {
    executive_summary: { text: string; check: Check } | null;
    top10: { text: string; citations: Citation[]; check: Check }[];
    units: { key: string; title: string; placeholder: boolean; summary: string; citations: Citation[];
             check: Check }[];
  };
  generated_at: string;
  verification: { verified: number; unverified: number; no_values: number } | null;
};

export type DocStatus = "uploaded" | "parsing" | "chunking" | "tagging" | "embedding" | "ready_for_insights"
  | "extracting" | "ready" | "failed";

export type ParseWarning = { code: string; message: string; severity: string; page?: number };

export type Doc = {
  id: string;
  title: string;
  status: DocStatus;
  progress: number;
  error: string | null;
  page_count: number | null;
  page_offset: number | null;
  parse_confidence: number | null;
  created_at: string;
  warning_count: number;
  parse_warnings?: ParseWarning[];
};

export type OutlineItem = {
  chunk_id: string;
  parent_id: string | null;
  clause_id: string | null;
  heading: string | null;
  level: number | null;
  kind: string;
  schedule_no: string | null;
  breadcrumb: string | null;
  page_pdf: number | null;
  page_printed: number | null;
};

export type Chunk = {
  chunk_id: string;
  clause_id: string | null;
  heading: string | null;
  breadcrumb: string | null;
  kind: string;
  page_pdf: number | null;
  page_end_pdf: number | null;
  page_printed: number | null;
  text: string;
  parent_id: string | null;
};

export type Message = {
  message_id: string;
  role: "user" | "assistant";
  content: string;
  citations: Citation[];
  feedback: number | null;
  check: Check;
  unknown_citations: string[] | null;
  created_at: string;
};

export type Conversation = { conversation_id: string; created_at: string; first_question: string | null };

export type ChatEvent =
  | { type: "conversation"; conversation_id: string }
  | { type: "token"; text: string }
  | { type: "done"; message_id: string; answer: string; citations: Citation[]; check: Check;
      unknown_citations: string[]; standalone_question: string | null }
  | { type: "error"; error: string };
