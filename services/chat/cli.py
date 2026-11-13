"""Chat with a stored document in the terminal (streams answers; Bedrock, about $0.002 per question).

    python -m chat.cli                                  # interactive, the owner's most recent document
    python -m chat.cli --ask "What is the base rate?" --ask "And for Gold Sites?"   # scripted follow-ups
    python -m chat.cli --conversation <id>              # continue a saved conversation

Run from the repo root with PYTHONPATH=services. Conversations are saved in Neon like the app will save them.
"""
from __future__ import annotations

import argparse
import sys

from chat.service import ChatError, ChatService
from shared.db import connect
from shared.embeddings import TitanEmbeddingProvider
from shared.llm import BedrockConverseProvider


def _turn(chat: ChatService, doc_id: str, owner: str, message: str, cid: str | None, verbose: bool) -> str:
    for ev in chat.ask(doc_id, owner, message, cid):
        if ev["type"] == "conversation":
            cid = ev["conversation_id"]
        elif ev["type"] == "token":
            print(ev["text"], end="", flush=True)
        else:
            print()
            if ev["standalone_question"]:
                print(f"  (searched for: {ev['standalone_question']})")
            cites = "; ".join(c["cite"] for c in ev["citations"]) or "none"
            print(f"  citations: {cites}")
            if ev["unknown_citations"]:
                print(f"  citations matching no retrieved clause: {ev['unknown_citations']}")
            if ev["check"]["status"] == "unverified":
                print(f"  UNVERIFIED numbers or dates: {', '.join(ev['check']['missing'])}")
            if verbose:
                m = ev["meta"]
                print(f"  {m['llm_calls']} LLM calls, {m['input_tokens']} in / {m['output_tokens']} out tokens; "
                      f"seconds {m['seconds']}")
    return cid


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m chat.cli", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--document", help="document ID (default: the owner's most recent document)")
    ap.add_argument("--owner", default="local-dev")
    ap.add_argument("--conversation", help="continue this conversation")
    ap.add_argument("--ask", action="append", help="ask this (repeat for follow-ups); omit for interactive mode")
    ap.add_argument("-v", "--verbose", action="store_true", help="show token use and timings")
    args = ap.parse_args(argv)

    with connect() as conn:
        doc_id = args.document or (conn.execute(
            "select id::text from documents where owner_id = %s and status = 'ready' order by created_at desc limit 1",
            (args.owner,)).fetchone() or [None])[0]
        if not doc_id:
            print("No ready document found for this owner.")
            return 1
        chat = ChatService(conn, BedrockConverseProvider(), TitanEmbeddingProvider())
        cid = args.conversation
        try:
            if args.ask:
                for q in args.ask:
                    print(f"\n> {q}")
                    cid = _turn(chat, doc_id, args.owner, q, cid, args.verbose)
            else:
                print("Ask about the contract (empty line to quit). AI-generated: verify against the document.")
                while (q := input("\n> ").strip()):
                    cid = _turn(chat, doc_id, args.owner, q, cid, args.verbose)
        except ChatError as e:
            print(f"Error: {e}")
            return 1
        print(f"\nconversation: {cid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
