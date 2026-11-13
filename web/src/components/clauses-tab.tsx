"use client";

import { ChevronRight, Search } from "lucide-react";
import { useContext, useEffect, useMemo, useState } from "react";

import { SourceContext } from "@/components/citations";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import type { OutlineItem } from "@/lib/types";

// The outline is a flat list of the clauses the document was split into, in reading order. The tree is rebuilt
// from each breadcrumb ("Title > Clause 2 Site Access > 2.1 Provision > 2.1.1"): a group like "Clause 2" may have
// no text of its own, so it only expands.
type Node = { name: string; item?: OutlineItem; children: Node[] };

function tree(items: OutlineItem[]): Node[] {
  const root: Node = { name: "", children: [] };
  for (const it of items) {
    const parts = (it.breadcrumb ?? it.heading ?? "").split(" > ").slice(1);
    let node = root;
    for (const part of parts.length ? parts : [it.heading ?? "(untitled)"]) {
      let child = node.children.find((c) => c.name === part);
      if (!child) node.children.push(child = { name: part, children: [] });
      node = child;
    }
    node.item = it;
  }
  return root.children;
}

function matches(n: Node, q: string): boolean {
  return n.name.toLowerCase().includes(q) || n.children.some((c) => matches(c, q));
}

function Row({ node, q, depth }: { node: Node; q: string; depth: number }) {
  const open = useContext(SourceContext);
  const [expanded, setExpanded] = useState(false);
  if (q && !matches(node, q)) return null;
  const hasKids = node.children.length > 0;
  const showKids = hasKids && (expanded || !!q);
  const it = node.item;
  const page = it?.page_printed ?? node.children.find((c) => c.item)?.item?.page_printed;
  const toggle = () => setExpanded(!expanded);
  const show = () => it && open({ chunk_id: it.chunk_id, clause_id: it.clause_id, label: node.name,
                                  page_pdf: it.page_pdf, page_end_pdf: null, page_printed: it.page_printed });
  return (
    <li>
      <div className="flex items-center gap-1 rounded-md hover:bg-muted/60" style={{ paddingLeft: depth * 16 }}>
        {hasKids ? (
          <button type="button" onClick={toggle} aria-expanded={showKids}
                  aria-label={`${showKids ? "Collapse" : "Expand"} ${node.name}`}
                  className="rounded p-1 text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring
                             focus-visible:outline-none">
            <ChevronRight className={`size-4 transition-transform ${showKids ? "rotate-90" : ""}`} aria-hidden />
          </button>
        ) : <span className="w-6 shrink-0" />}
        <button type="button" onClick={it ? show : toggle}
                className={`flex min-w-0 flex-1 items-baseline gap-2 rounded px-1 py-1.5 text-left text-sm
                            focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none
                            ${depth === 0 ? "font-medium" : ""}`}
                aria-label={it ? `Open ${node.name}` : undefined}>
          <span className="truncate">{node.name}</span>
          {page && <span className="ml-auto shrink-0 text-xs text-muted-foreground">p. {page}</span>}
        </button>
      </div>
      {showKids && (
        <ul>{node.children.map((c, i) => <Row key={`${c.name}-${i}`} node={c} q={q} depth={depth + 1} />)}</ul>
      )}
    </li>
  );
}

export function ClausesTab({ documentId }: { documentId: string }) {
  const [items, setItems] = useState<OutlineItem[] | null>(null);
  const [q, setQ] = useState("");
  useEffect(() => { api.outline(documentId).then(setItems).catch(() => setItems([])); }, [documentId]);
  const roots = useMemo(() => tree(items ?? []), [items]);

  return (
    <div className="flex flex-col gap-3">
      <div className="relative max-w-sm">
        <Search className="absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
        <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search clauses and schedules"
               className="pl-8" aria-label="Search clauses and schedules" />
      </div>
      {items === null ? <Skeleton className="h-64 w-full" /> : (
        <ul className="rounded-lg border p-2">
          {roots.map((n, i) => <Row key={`${n.name}-${i}`} node={n} q={q.trim().toLowerCase()} depth={0} />)}
        </ul>
      )}
    </div>
  );
}
