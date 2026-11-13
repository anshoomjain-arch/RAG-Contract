"use client";

import { ChevronDown, ShieldCheck } from "lucide-react";

import { Citations } from "@/components/citations";
import { CheckFlag } from "@/components/status";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { Field, Insights, Item } from "@/lib/types";

function Verification({ insights }: { insights: Insights }) {
  const v = insights.verification;
  if (!v) return null;
  return (
    <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
      <ShieldCheck className="size-4" aria-hidden />
      {v.verified} values checked against their source; {v.unverified} flagged as unverified.
      Generated {new Date(insights.generated_at).toLocaleString()}.
    </p>
  );
}

export function SummaryTab({ insights }: { insights: Insights }) {
  const { executive_summary: exec, top10, units } = insights.summary;
  return (
    <div className="flex flex-col gap-8">
      <Verification insights={insights} />
      {exec && (
        <section aria-labelledby="exec" className="flex flex-col gap-2">
          <h2 id="exec" className="text-xl font-semibold">Executive summary</h2>
          <p className="max-w-prose leading-relaxed">{exec.text} <CheckFlag check={exec.check} /></p>
        </section>
      )}
      <section aria-labelledby="top10" className="flex flex-col gap-3">
        <h2 id="top10" className="text-xl font-semibold">Top {top10.length} things to know</h2>
        <ol className="flex flex-col gap-3">
          {top10.map((t, i) => (
            <li key={i} className="flex gap-3">
              <span className="mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full bg-muted
                               text-xs font-semibold" aria-hidden>{i + 1}</span>
              <div className="flex flex-col gap-1">
                <p className="leading-relaxed">{t.text} <CheckFlag check={t.check} /></p>
                <Citations citations={t.citations} />
              </div>
            </li>
          ))}
        </ol>
      </section>
      <section aria-labelledby="units" className="flex flex-col gap-2">
        <h2 id="units" className="text-xl font-semibold">Clause by clause</h2>
        <div className="flex flex-col divide-y rounded-lg border">
          {units.map((u) => (
            <details key={u.key} className="group px-4 py-3">
              <summary className="flex cursor-pointer list-none items-center justify-between gap-3 rounded-sm
                                  font-medium focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
                <span className={u.placeholder ? "text-muted-foreground" : ""}>{u.title}</span>
                <ChevronDown className="size-4 shrink-0 transition-transform group-open:rotate-180" aria-hidden />
              </summary>
              <div className="mt-2 flex flex-col gap-2 text-sm">
                <p className="leading-relaxed text-muted-foreground">{u.summary} <CheckFlag check={u.check} /></p>
                <Citations citations={u.citations} />
              </div>
            </details>
          ))}
        </div>
      </section>
    </div>
  );
}

function ItemRow({ item }: { item: Item }) {
  return (
    <li className="flex flex-col gap-1">
      <p className="leading-relaxed">{item.value} <CheckFlag check={item.check} /></p>
      {item.quotes?.[0] && (
        <blockquote className="border-l-2 pl-3 text-xs leading-relaxed text-muted-foreground italic">
          “{item.quotes[0]}”
        </blockquote>
      )}
      <Citations citations={item.citations} quotes={item.quotes} />
    </li>
  );
}

function FieldRow({ field }: { field: Field }) {
  return (
    <div className="grid gap-1 py-3 sm:grid-cols-[12rem_1fr] sm:gap-4">
      <dt className="text-sm font-medium text-muted-foreground">{field.label}</dt>
      <dd>
        {field.items?.length
          ? <ul className="flex flex-col gap-3">{field.items.map((it, i) => <ItemRow key={i} item={it} />)}</ul>
          : <span className="text-sm text-muted-foreground italic">Not stated in the document</span>}
      </dd>
    </div>
  );
}

export function KeyInfoTab({ insights }: { insights: Insights }) {
  const { sections, observations, missing } = insights.key_info;
  return (
    <div className="flex flex-col gap-5">
      <Verification insights={insights} />
      {sections.map((s) => (
        <Card key={s.key}>
          <CardHeader><CardTitle><h2 className="text-lg font-semibold">{s.title}</h2></CardTitle></CardHeader>
          <CardContent>
            <dl className="divide-y">{s.fields.map((f) => <FieldRow key={f.key} field={f} />)}</dl>
          </CardContent>
        </Card>
      ))}
      {observations.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle><h2 className="text-lg font-semibold">Unusual or one-sided terms</h2></CardTitle>
            <p className="text-xs text-muted-foreground">The model&apos;s observations, not legal advice.</p>
          </CardHeader>
          <CardContent><ul className="flex flex-col gap-4">
            {observations.map((o, i) => <ItemRow key={i} item={o} />)}
          </ul></CardContent>
        </Card>
      )}
      {missing.length > 0 && (
        <Card>
          <CardHeader><CardTitle><h2 className="text-lg font-semibold">Not included in this document</h2>
          </CardTitle></CardHeader>
          <CardContent><ul className="flex flex-col gap-3">
            {missing.map((m, i) => <ItemRow key={i} item={m} />)}
          </ul></CardContent>
        </Card>
      )}
    </div>
  );
}
