"""Parser debugging CLI.

    python -m ingest.cli samples/contract.pdf --dry-run            # report only
    python -m ingest.cli samples/contract.pdf --out out/chunks.jsonl

Run from the repo root with PYTHONPATH=services. Exit code 1 when the parse has error-level warnings
(e.g. a clause from the table of contents was not found), so problems are never silent.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import Counter
from pathlib import Path

from ingest.chunker.chunker import check_lead_ins, chunk_document, nice
from ingest.models import Node, ParseResult
from ingest.pipeline import parse_document
from ingest.tagger.tag import apply_rule_tags


def _pages(res: ParseResult, node: Node) -> str:
    p = res.printed_page(node.page)
    return f"p.{p} (PDF {node.page})" if p is not None else f"PDF p.{node.page}"


def print_tree(res: ParseResult, depth: int) -> None:
    def_counts = Counter(id(d.node) for d in res.definitions)

    def walk(node: Node, indent: int) -> None:
        for c in node.children:
            if c.kind in ("clause", "paragraph") and c.level > depth:
                continue
            label = {"clause": f"{c.number}", "paragraph": f"{c.number}", "schedule": f"Schedule {c.number}",
                     "part": "", "form": "Form:", "preamble": "", "execution": ""}[c.kind]
            extra = []
            if def_counts.get(id(c)):
                extra.append(f"{def_counts[id(c)]} definitions")
            if c.tables:
                extra.append(f"{len(c.tables)} table(s)")
            if c.placeholder:
                extra.append("PLACEHOLDER")
            tail = f"  [{', '.join(extra)}]" if extra else ""
            print(f"{'  ' * indent}{label} {nice(c.heading)}".rstrip() + f"  {_pages(res, c)}{tail}")
            walk(c, indent + 1)

    walk(res.root, 1)


def report(res: ParseResult, chunks: list[dict], depth: int) -> None:
    s = res.stats
    toc = s["toc"]
    print(f"Document : {res.title}  ({s['pages']} pages, {s['front_matter_pages']} front-matter pages)")
    if res.page_offset is not None:
        n = sum(1 for p in res.pages if p.printed is not None)
        print(f"Page offset: PDF page = printed page + {res.page_offset}  (from {n} footer page numbers)")
    else:
        print("Page offset: not found")
    print(f"TOC check : {toc['matched_clauses']}/{toc['toc_clauses']} clauses, "
          f"{toc['matched_schedules']}/{toc['toc_schedules']} schedules matched; "
          f"{len(toc['title_mismatch'])} title mismatches, {len(toc['page_mismatch'])} page mismatches, "
          f"{len(toc['missing'])} missing, {len(toc['not_in_toc'])} not in TOC")
    cl, sp = s["clauses_by_level"], s["schedule_paragraphs_by_level"]
    print(f"Clauses   : {cl.get(1, 0)} top-level; sub-clauses by level {cl}")
    print(f"Schedules : {s['schedules']}; schedule paragraphs by level {sp}")
    print(f"List items: {s['list_items']} ((a), (i), (A), a. ...)")
    print(f"Definitions: {s['definitions']}")
    print(f"Placeholders: {', '.join(s['placeholders']) or 'none'}")
    print(f"Forms     : {', '.join(s['forms']) or 'none'}")
    if s["parts"]:
        print(f"Parts     : {', '.join(s['parts'])}")
    print(f"Tables    : {s['tables']} on {len(s['table_pages'])} PDF pages; "
          f"{s['form_templates']} blank form templates; {s['tables_needing_vision']} flagged as garbled")
    for t in res.tables:
        r = len(t.rows)
        c = max((len(x) for x in t.rows), default=0)
        flag = ("  NEEDS VISION" if t.needs_vision else "") + ("  blank form template" if t.kind == "form_template" else "") + ("  (re-read from text)" if t.method == "text" else "")
        print(f"    PDF p.{t.page:<4} {r}x{c}  quality {t.quality:.2f}{flag}")
    print("Short pages:", ", ".join(f"PDF {p['page']} ({p['chars']} chars{', image' if p['image'] else ''})"
                                    for p in s["short_pages"]) or "none")

    kinds = Counter(c["chunk_kind"] for c in chunks)
    roles = Counter(c["role"] for c in chunks)
    parents = [c for c in chunks if c["role"] in ("parent", "both")]
    kids = [c for c in chunks if c["role"] in ("child", "both")]
    print(f"Chunks    : {len(chunks)} total; roles {dict(roles)}; kinds {dict(kinds)}")
    if kids:
        kt = sorted(c["tokens"] for c in kids if c["chunk_kind"] != "definition")
        print(f"            embedded chunk tokens (excl. definitions): median {kt[len(kt) // 2]}, max {kt[-1]}; "
              f"largest parent {max(c['tokens'] for c in parents)} tokens")
    problems = check_lead_ins(chunks)
    print(f"Lead-in check: {'OK' if not problems else f'{len(problems)} problem(s)'}")
    for p in problems[:10]:
        print("    ", p)
    print(f"Parse confidence: {res.parse_confidence}")

    print(f"\nClause tree (to level {depth}):")
    print_tree(res, depth)

    shown = [w for w in res.warnings if w.severity != "info" or w.code != "table_low_quality"]
    print(f"\nWarnings ({len(res.warnings)}):")
    for w in sorted(shown, key=lambda w: ({"error": 0, "warning": 1, "info": 2}[w.severity], w.page or 0)):
        if w.code == "table_low_quality":
            continue  # already listed with the tables above
        print(f"  [{w.severity}] {w.code}: {w.message}")


def tag_report(chunks: list[dict], res: ParseResult, spot: int, seed: int) -> None:
    s = apply_rule_tags(chunks, res)
    print("\nRule-based tags (embedded chunks):")
    print(f"  Cross-references: {s['mentions']} mentions -> {s['targets']} targets; {s['resolved']} resolved, "
          f"{len(s['unresolved'])} unresolved, {s['external']} to other documents (not resolved), "
          f"{s['broad']} broad (e.g. all of Clause 1.1)")
    print(f"  Chunks with refs_out: {s['with_refs_out']}, with refs_in: {s['with_refs_in']}")
    print(f"  Defined terms found in {s['with_terms']} chunks; values in {s['with_values']} chunks {s['value_types']}")
    print(f"  Modality: {s['modality']}")
    if s["unresolved"]:
        print("  Unresolved references:")
        for u in s["unresolved"]:
            print("    ", u)
    if spot:
        embedded = [c for c in chunks if c["role"] != "parent"]
        print(f"\nSpot check: {spot} random chunks (seed {seed})")
        for c in random.Random(seed).sample(embedded, min(spot, len(embedded))):
            vals = ", ".join(v["text"] for v in c["values"][:6])
            print(f"\n  [{c['chunk_kind']}] {c['clause_id']}  PDF p.{c['page_pdf']}  {c['breadcrumb']}")
            print(f"    text     : {c['text'][:160]!r}")
            print(f"    modality : {c['modality']} {c['modality_counts'] or ''}")
            print(f"    refs_out : {c['refs_out']}  refs_in: {c['refs_in'][:8]}{' ...' if len(c['refs_in']) > 8 else ''}")
            terms = c["defined_terms_used"]
            print(f"    terms    : {terms[:10]}{f' (+{len(terms) - 10})' if len(terms) > 10 else ''}")
            print(f"    values   : {vals or '-'}")


# Bedrock on-demand prices (USD per 1K tokens), us-east-1, from the AWS Pricing API; override via env if they change.
PRICE_IN = float(os.environ.get("LLM_PRICE_IN_PER_1K", "0.00015"))
PRICE_OUT = float(os.environ.get("LLM_PRICE_OUT_PER_1K", "0.0006"))


def llm_report(chunks: list[dict], res: ParseResult, how_many: str, seed: int) -> None:
    from shared.llm import BedrockConverseProvider
    from ingest.tagger.llm import LLMTagger, apply_outcomes

    embedded = [c for c in chunks if c["role"] != "parent"]
    sample = embedded if how_many == "all" else random.Random(seed).sample(embedded, min(int(how_many), len(embedded)))
    provider = BedrockConverseProvider()
    tagger = LLMTagger(provider, res)
    print(f"\nLLM tags with {provider.model_id}: {len(sample)} of {len(embedded)} embedded chunks")
    print(f"  parties: {tagger.parties}; topic vocabulary ({len(tagger.vocab)}): {', '.join(tagger.vocab)}")
    t0 = time.time()
    outcomes = tagger.tag_many(sample)
    secs = time.time() - t0
    apply_outcomes(chunks, outcomes)
    by_id = {c["id"]: c for c in chunks}
    for o in outcomes:
        c = by_id[o.chunk_id]
        status = "ok" if o.ok else f"FAILED ({o.error})"
        retry = f", {o.attempts} attempts" if o.attempts > 1 else ""
        print(f"  [{c['chunk_kind']}] {c['clause_id']:<14} PDF p.{c['page_pdf']:<4} topic={o.topic} "
              f"party={o.party_obligor} [{status}{retry}]  {c['heading'][:40]!r}")
    tin = sum(o.input_tokens for o in outcomes)
    tout = sum(o.output_tokens for o in outcomes)
    ok = sum(o.ok for o in outcomes)
    cost = tin / 1000 * PRICE_IN + tout / 1000 * PRICE_OUT
    print(f"\n  valid: {ok}/{len(outcomes)}; retries: {sum(o.attempts - 1 for o in outcomes)}; time {secs:.1f}s")
    print(f"  tokens: {tin} in, {tout} out; cost ${cost:.4f}")
    if how_many != "all" and outcomes:
        scale = len(embedded) / len(outcomes)
        print(f"  estimate for all {len(embedded)} embedded chunks: ~{int(tin * scale)} in + ~{int(tout * scale)} out "
              f"tokens = ~${cost * scale:.3f}; ~{secs * scale / 60:.1f} min at 4 parallel calls")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ingest.cli", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf")
    ap.add_argument("--dry-run", action="store_true", help="print the report; write nothing")
    ap.add_argument("--out", help="write chunks as JSON lines to this path (default out/<name>.chunks.jsonl)")
    ap.add_argument("--depth", type=int, default=2, help="clause tree depth to print (default 2)")
    ap.add_argument("--tag", action="store_true", help="add rule-based tags and cross-references (no network)")
    ap.add_argument("--spot-check", type=int, default=0, metavar="N",
                    help="print the tags of N random embedded chunks (with --tag)")
    ap.add_argument("--seed", type=int, default=7, help="random seed for --spot-check and --llm-tags")
    ap.add_argument("--llm-tags", metavar="N|all",
                    help="add Qwen topic/party tags (Bedrock, costs money) to N random embedded chunks, or all")
    args = ap.parse_args(argv)

    res = parse_document(args.pdf)
    chunks = chunk_document(res)
    report(res, chunks, args.depth)
    if args.tag or args.llm_tags:
        tag_report(chunks, res, args.spot_check, args.seed)
    if args.llm_tags:
        llm_report(chunks, res, args.llm_tags, args.seed)

    if not args.dry_run:
        out = Path(args.out or Path("out") / f"{Path(args.pdf).stem}.chunks.jsonl")
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as f:
            for c in chunks:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        print(f"\nWrote {len(chunks)} chunks to {out}")
    return 1 if any(w.severity == "error" for w in res.warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
