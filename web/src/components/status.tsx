"use client";

import { AlertTriangle, CheckCircle2, Loader2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { Check, DocStatus } from "@/lib/types";

// The processing steps shown to the user (SPEC section 10), and which step each stored status belongs to.
export const STEPS = ["Uploaded", "Parsing", "Chunking", "Tagging", "Embedding", "Extracting", "Ready"] as const;
const STEP_OF: Record<DocStatus, number> = {
  uploaded: 0, parsing: 1, chunking: 2, tagging: 3, embedding: 4, ready_for_insights: 5, extracting: 5, ready: 6,
  failed: -1,
};

export function stepOf(status: DocStatus): number {
  return STEP_OF[status] ?? 0;
}

export function StatusChip({ status }: { status: DocStatus }) {
  if (status === "ready") {
    return <Badge variant="secondary"><CheckCircle2 aria-hidden />Ready</Badge>;
  }
  if (status === "failed") {
    return <Badge variant="destructive"><AlertTriangle aria-hidden />Failed</Badge>;
  }
  return (
    <Badge variant="outline">
      <Loader2 className="animate-spin" aria-hidden />
      {STEPS[stepOf(status)]}
    </Badge>
  );
}

/** A small flag on values whose numbers or dates were not found in the cited text. */
export function CheckFlag({ check }: { check: Check }) {
  if (!check || check.status !== "unverified") return null;
  return (
    <Tooltip>
      <TooltipTrigger render={
        <span tabIndex={0} className="inline-flex items-center gap-1 rounded-sm bg-amber-100 px-1.5 py-0.5 text-[11px]
                                      font-medium text-amber-900 dark:bg-amber-500/15 dark:text-amber-300" />
      }>
        <AlertTriangle className="size-3" aria-hidden />
        Unverified
      </TooltipTrigger>
      <TooltipContent>
        Not found in the cited text: {check.missing.join(", ")}. Check the source before relying on it.
      </TooltipContent>
    </Tooltip>
  );
}
