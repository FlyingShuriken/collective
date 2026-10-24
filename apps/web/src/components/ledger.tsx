"use client";

import { useEffect, useState } from "react";

import { SelfAudit } from "@/components/self-audit";
import { SourceViewer } from "@/components/source-viewer";
import { Button } from "@/components/ui/button";
import { apiRequest } from "@/lib/api/client";
import type { components } from "@/lib/api/generated";
import { money } from "@/lib/format";
import { messages, type Locale } from "@/lib/i18n";

export function Ledger({ business, locale }: { business: string; locale: Locale }) {
  const text = messages[locale].ledger;
  const [kind, setKind] = useState("purchase");
  const [cursor, setCursor] = useState<string | null>(null);
  const [page, setPage] = useState<components["schemas"]["LedgerPage"] | null>(null);
  const [source, setSource] = useState<string | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    apiRequest<components["schemas"]["LedgerPage"]>(`/v1/businesses/${business}/ledger?kind=${kind}${cursor ? `&cursor=${cursor}` : ""}`, { signal: controller.signal })
      .then((result) => { if (!controller.signal.aborted) setPage(result); })
      .catch(() => { if (!controller.signal.aborted) setError(true); });
    return () => controller.abort();
  }, [business, kind, cursor]);
  return <div className="space-y-6">
    <SelfAudit key={business} business={business} locale={locale} />
    <section className="space-y-4 rounded-xl border bg-card p-5" aria-label={text.title}>
      <h2 className="font-serif text-2xl">{text.title}</h2>
      <label className="block text-sm">{text.type}<select className="mt-2 min-h-11 w-full rounded-md border bg-card px-3" value={kind} onChange={(event) => {
        setKind(event.target.value); setCursor(null); setPage(null); setSource(null); setError(false);
      }}>{(["purchase", "stock", "quote", "po"] as const).map((value) => <option key={value} value={value}>{text.kinds[value]}</option>)}</select></label>
      {error && <p role="alert">{messages[locale].unavailable}</p>}
      {!page && !error && <p role="status">{messages[locale].loading}</p>}
      {page?.items.length === 0 && <p>{text.empty} <a className="underline" href={`/${locale}/inbox?business=${business}`}>{text.next}</a></p>}
      <ul className="space-y-3">{page?.items.map((item) => <li key={item.id} className="space-y-2 rounded-md border p-3 text-sm">
        <p className="font-medium">{text.kinds[item.kind]}</p>
        <p>{new Date(item.recorded_at).toLocaleString(locale)} · {text.revision} {item.revision}</p>
        {item.quantity !== null && <p>{text.quantity}: {item.quantity}</p>}
        {item.price_visibility === "visible" ? <>
          <p>{text.total}: {item.total === null ? text.unknown : money(item.total)}</p>
          {item.source_id && <Button variant="outline" onClick={() => setSource(item.source_id)}>{messages[locale].viewSource}</Button>}
        </> : <p>{messages[locale].audit.hidden}</p>}
      </li>)}</ul>
      {page?.next_cursor && <Button variant="outline" onClick={() => { setCursor(page.next_cursor); setPage(null); }}>{messages[locale].nextPage}</Button>}
      {source && <SourceViewer key={source} business={business} sourceId={source} locale={locale} />}
    </section>
  </div>;
}
