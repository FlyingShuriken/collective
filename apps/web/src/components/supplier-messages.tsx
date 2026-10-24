"use client";

import { useEffect, useState } from "react";

import { DraftCard } from "@/components/draft-card";
import { Button } from "@/components/ui/button";
import { apiRequest } from "@/lib/api/client";
import type { components } from "@/lib/api/generated";
import { messages, type Locale } from "@/lib/i18n";

export function SupplierMessages({ business, locale, owner, need }: {
  business: string; locale: Locale; owner: boolean; need?: components["schemas"]["NeedView"];
}) {
  const text = messages[locale].communication;
  const [page, setPage] = useState<components["schemas"]["DraftPage"] | null>(null);
  const [suppliers, setSuppliers] = useState<components["schemas"]["SupplierOption"][]>([]);
  const [supplier, setSupplier] = useState("");
  const [cursor, setCursor] = useState<string | null>(null);
  const [job, setJob] = useState<components["schemas"]["JobStatus"] | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const root = `/v1/businesses/${business}` as const;
  useEffect(() => {
    const controller = new AbortController();
    const query = new URLSearchParams();
    if (cursor) query.set("cursor", cursor);
    if (need) query.set("reference_id", need.id);
    Promise.all([
      apiRequest<components["schemas"]["DraftPage"]>(`${root}/drafts?${query}`, { signal: controller.signal }),
      apiRequest<components["schemas"]["RecordOptions"]>(`${root}/record-options`, { signal: controller.signal }),
    ]).then(([drafts, options]) => {
      if (!controller.signal.aborted) { setPage(drafts); setSuppliers(options.suppliers); }
    }).catch(() => { if (!controller.signal.aborted) setError(text.failed); });
    return () => controller.abort();
  }, [root, need, cursor, refresh, text.failed]);
  useEffect(() => {
    if (!job || ["succeeded", "failed"].includes(job.status)) return;
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const result = await apiRequest<components["schemas"]["JobStatus"]>(`${root}/jobs/${job.id}`, { signal: controller.signal });
        if (!controller.signal.aborted) {
          setJob(result);
          if (result.status === "succeeded") setRefresh((value) => value + 1);
        }
      } catch { if (!controller.signal.aborted) setError(text.failed); }
    }, 1500);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [root, job, text.failed]);

  async function request(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!need) return;
    setPending(true); setError("");
    try {
      setJob(await apiRequest<components["schemas"]["JobStatus"]>(`${root}/drafts`, {
        method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ kind: "rfq", reference_id: need.id, expected_revision: need.revision,
          supplier_id: supplier }),
      }));
    } catch { setError(text.failed); } finally { setPending(false); }
  }

  return <section className="space-y-4 rounded-lg border p-4" aria-label={text.title}>
    <h2 className="font-serif text-2xl">{text.title}</h2>
    <p className="text-sm text-muted-foreground">{text.explanation}</p>
    {owner && need && ["selected", "reserved"].includes(need.state) && <form className="space-y-3" onSubmit={(event) => void request(event)}>
      <label className="block text-sm">{text.supplier}<select required className="mt-2 min-h-11 w-full rounded-md border bg-card px-3" value={supplier} onChange={(event) => setSupplier(event.target.value)}><option value="">{text.select}</option>{suppliers.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
      <Button type="submit" disabled={pending || !!job && !["succeeded", "failed"].includes(job.status)}>{text.request}</Button>
    </form>}
    {error && <p role="alert">{error}</p>}
    {job && !["succeeded", "failed"].includes(job.status) && <p role="status">{text.working}</p>}
    {job?.status === "failed" && <p role="status">{text.missing}</p>}
    {!page && !error && <p role="status">{messages[locale].loading}</p>}
    {page?.items.length === 0 && <p>{text.empty}</p>}
    {page?.items.map((draft) => <DraftCard key={draft.id} business={business} draftId={draft.id} locale={locale} owner={owner} onChanged={() => setRefresh((value) => value + 1)} />)}
    {page?.next_cursor && <Button variant="outline" onClick={() => { setCursor(page.next_cursor ?? null); setPage(null); }}>{messages[locale].nextPage}</Button>}
    <Button variant="outline" onClick={() => { setError(""); setRefresh((value) => value + 1); }}>{text.refresh}</Button>
  </section>;
}
