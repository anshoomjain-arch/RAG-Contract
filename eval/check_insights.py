"""Check stored insights against eval/golden_insights.yaml (no LLM calls, no cost).

    python eval/check_insights.py          # pass/fail per check, verification counts, every unverified value
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))

from run_eval import matches  # noqa: E402
from shared.db import connect  # noqa: E402


def _norm(text: str) -> str:
    return re.sub(r"\s*per\s?cent\b", "%", text.lower())


def judge(items: list[dict], contains: list[str], cited: list[str]) -> str:
    """Each expected string must appear in an item that cites an expected clause and is not unverified (the
    strings may come from different items, e.g. one item per party)."""
    for want in contains:
        options = want if isinstance(want, list) else [want]
        want = " / ".join(options)
        found = [it for it in items if any(_norm(o) in _norm(it["value"]) for o in options)]
        if not found:
            return f"NOT FOUND ({want})"
        right = [it for it in found if any(matches(c["clause_id"] or "", e) for c in it["citations"] for e in cited)]
        if not right:
            return "WRONG CITATION (" + "; ".join(c["label"] for c in found[0]["citations"]) + ")"
        if all((it.get("check") or {}).get("status") == "unverified" for it in right):
            return f"UNVERIFIED ({want})"
    return "PASS"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default=str(ROOT / "eval" / "golden_insights.yaml"))
    ap.add_argument("--owner", default="local-dev")
    args = ap.parse_args(argv)

    spec = yaml.safe_load(Path(args.file).read_text(encoding="utf-8"))
    with connect() as conn:
        row = conn.execute(
            """select i.key_info, i.meta from insights i join documents d on d.id = i.document_id
               where d.owner_id = %s and d.title = %s order by d.created_at desc limit 1""",
            (args.owner, spec["document"])).fetchone()
    if not row:
        print(f"No insights for {spec['document']!r}; run `python -m insights.run` first.")
        return 1
    key_info, meta = row
    fields = {f["key"]: f["items"] or [] for s in key_info["sections"] for f in s["fields"]}
    fields["missing"] = key_info["missing"]

    passed = 0
    print(f"Key-information checks ({spec['document']}):")
    for c in spec["checks"]:
        verdict = judge(fields.get(c["field"], []), c["contains"], c["cited"])
        passed += verdict == "PASS"
        wanted = ", ".join(" / ".join(w) if isinstance(w, list) else w for w in c["contains"])
        print(f"  {verdict:10.10}  {c['field']}: {wanted}"
              + (f"  -> {verdict}" if verdict != "PASS" else ""))
    print(f"\n{passed}/{len(spec['checks'])} passed")

    print(f"\nVerification of numbers and dates: {meta['verification']}")
    flagged = [(s["title"], f["label"], it) for s in key_info["sections"] for f in s["fields"]
               for it in f["items"] or [] if it["check"]["status"] == "unverified"]
    flagged += [("Observations", "", o) for o in key_info["observations"] if o["check"]["status"] == "unverified"]
    for section, label, it in flagged:
        cites = "; ".join(c["label"] for c in it["citations"])
        print(f"  UNVERIFIED {section} / {label}: missing {', '.join(it['check']['missing'])} in [{cites}]")
    return 0 if passed == len(spec["checks"]) else 1


if __name__ == "__main__":
    sys.exit(main())
