"use client";

import { useEffect, useState } from "react";

import { SourceViewer } from "@/components/source-viewer";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError, apiRequest } from "@/lib/api/client";
import type { components } from "@/lib/api/generated";
import { messages, type Locale } from "@/lib/i18n";

type Quote = components["schemas"]["QuoteProposal"];
type Reading = components["schemas"]["QuoteReading"];
type Job = components["schemas"]["JobStatus"];
type Source = components["schemas"]["VisibleSource"];

export function QuoteReview({ business, source, locale, owner, onChanged }: {
  business: string; source: Source; locale: Locale; owner: boolean; onChanged: () => void;
}) {
  const text = messages[locale].quoteReview;
  const [proposal, setProposal] = useState<Quote | null>(null);
  const [reading, setReading] = useState<Reading | null>(null);
  const [options, setOptions] = useState<components["schemas"]["RecordOptions"] | null>(null);
  const [needs, setNeeds] = useState<components["schemas"]["NeedView"][]>([]);
  const [quotes, setQuotes] = useState<components["schemas"]["VisibleLedgerEntry"][]>([]);
  const [job, setJob] = useState<Job | null>(null);
  const [supplier, setSupplier] = useState("");
  const [need, setNeed] = useState("");
  const [replace, setReplace] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const root = `/v1/businesses/${business}` as const;

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      apiRequest<components["schemas"]["ProposalPage"]>(`${root}/proposals?source_id=${source.id}`, { signal: controller.signal }),
      apiRequest<components["schemas"]["RecordOptions"]>(`${root}/record-options`, { signal: controller.signal }),
      apiRequest<components["schemas"]["NeedPage"]>(`${root}/needs`, { signal: controller.signal }),
      apiRequest<components["schemas"]["LedgerPage"]>(`${root}/ledger?kind=quote&limit=100`, { signal: controller.signal }),
    ]).then(([proposals, available, requested, ledger]) => {
      if (controller.signal.aborted) return;
      setOptions(available); setNeeds(requested.items);
      setQuotes(ledger.items.filter((item): item is components["schemas"]["VisibleLedgerEntry"] => item.price_visibility === "visible"));
      const current = proposals.items.find((item) => item.kind === "quote" && item.price_visibility === "visible");
      if (current?.kind === "quote" && current.price_visibility === "visible") {
        setProposal(current); setReading(current.reading); setAcknowledged(false);
      }
    }).catch(() => { if (!controller.signal.aborted) setError(text.failed); });
    return () => controller.abort();
  }, [root, source.id, refresh, text.failed]);

  useEffect(() => {
    if (!job || ["succeeded", "failed"].includes(job.status)) return;
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const current = await apiRequest<Job>(`${root}/jobs/${job.id}`, { signal: controller.signal });
        if (!controller.signal.aborted) {
          setJob(current);
          if (current.status === "succeeded") setRefresh((value) => value + 1);
        }
      } catch { if (!controller.signal.aborted) setError(text.failed); }
    }, 1500);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [root, job, text.failed]);

  function change<K extends keyof Reading>(key: K, value: Reading[K]) {
    setAcknowledged(false);
    setReading((current) => current ? { ...current, [key]: value,
      issues: current.issues?.filter((issue) => issue.field_path !== key && !issue.field_path.startsWith(`${key}.`)),
    } : current);
  }

  async function request(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const selected = needs.find((item) => item.id === need);
    if (!selected) return;
    setPending(true); setError("");
    try {
      setJob(await apiRequest<Job>(`${root}/quote-proposals`, {
        method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ source_id: source.id, source_sha256: source.sha256, supplier_id: supplier,
          spec_id: selected.spec_id, spec_revision: selected.spec_revision }),
      }));
    } catch { setError(text.failed); } finally { setPending(false); }
  }

  async function mutate(action: "edit" | "confirm" | "reject") {
    if (!proposal || !reading) return;
    setPending(true); setError("");
    const selected = quotes.find((item) => item.id === replace);
    const body = action === "edit" ? { expected_revision: proposal.current_revision, reading }
      : { expected_revision: proposal.current_revision, acknowledged_flags: proposal.flags,
        ...(action === "confirm" && selected ? { quote_id: selected.id, expected_quote_revision: selected.revision } : {}) };
    try {
      const result = await apiRequest<Quote>(`${root}/proposals/${proposal.id}${action === "edit" ? "" : `/${action}`}`, {
        method: action === "edit" ? "PATCH" : "POST", headers: { "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify(body),
      });
      setProposal(result); setReading(result.reading); setAcknowledged(false); onChanged();
    } catch (error) { setError(error instanceof ApiError && error.status === 409 ? text.stale : text.failed); }
    finally { setPending(false); }
  }

  const inputClass = "mt-2 min-h-11 w-full rounded-md border bg-card px-3";
  const dirty = !!reading && !!proposal && JSON.stringify(reading) !== JSON.stringify(proposal.reading);
  const labels: Record<string, string> = text.fields;
  return <section className="space-y-4 rounded-lg border p-4" aria-label={text.title}>
    <h3 className="font-semibold">{text.title}</h3>
    <p className="text-sm text-muted-foreground">{text.explanation}</p>
    {error && <p role="alert" className="text-destructive">{error}</p>}
    {!owner && <p>{text.owner}</p>}
    {owner && !proposal && <form className="space-y-3" onSubmit={(event) => void request(event)}>
      <label className="block text-sm">{text.supplier}<select required className={inputClass} value={supplier} onChange={(event) => setSupplier(event.target.value)}>
        <option value="">{text.select}</option>{options?.suppliers.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select></label>
      <label className="block text-sm">{text.product}<select required className={inputClass} value={need} onChange={(event) => setNeed(event.target.value)}>
        <option value="">{text.select}</option>{needs.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select></label>
      {!needs.length && <p>{text.noProduct} <a className="underline" href={`/${locale}/approvals?business=${business}`}>{messages[locale].screens.approvals}</a></p>}
      <Button type="submit" disabled={pending || !!job && !["failed", "succeeded"].includes(job.status)}>{text.request}</Button>
    </form>}
    {job && !["succeeded", "failed"].includes(job.status) && <p role="status">{text.working}</p>}
    {job?.status === "failed" && <p role="status">{text.replayMiss}</p>}
    {proposal && reading && <>
      <p role="status">{proposal.status === "confirmed" ? text.confirmed : proposal.status === "rejected" ? text.rejected : text.proposed}</p>
      <p className="text-sm">{text.revision} {proposal.current_revision}</p>
      <p className="text-sm">{proposal.interpretation_mode === "replay" ? messages[locale].records.replayReading : text.explanation}</p>
      {proposal.flags.length > 0 && <p className="text-sm">{text.flags} ({proposal.flags.length})</p>}
      {owner && proposal.status === "pending_review" ? <>
        <form className="space-y-4" aria-label={text.edit} onSubmit={(event) => { event.preventDefault(); void mutate("edit"); }}>
          <fieldset disabled={pending} className="grid gap-3 sm:grid-cols-2">
            {(["description", "currency", "pack_multiple", "per_business_moq", "aggregate_moq", "delivery_location", "delivery_fee", "free_delivery_threshold", "tax_rate", "discount", "surcharge", "validity_text", "conditions"] as const).map((key) => <label key={key} className="block text-sm">{labels[key]}
              <Input className="mt-2" value={reading[key] ?? ""} onChange={(event) => change(key, event.target.value || null)} />
            </label>)}
            <label className="block text-sm">{labels.credit_days}<Input type="number" min={0} max={3650} step={1} value={reading.credit_days ?? ""} onChange={(event) => change("credit_days", event.target.value === "" ? null : Number(event.target.value))} /></label>
            {(["base_unit", "tier_kind", "delivery_mode"] as const).map((key) => <label key={key} className="block text-sm">{labels[key]}<select className={inputClass} value={reading[key] ?? ""} onChange={(event) => change(key, (event.target.value || null) as Reading[typeof key])}>
              <option value="">{text.unknown}</option>{(key === "base_unit" ? ["piece", "ml", "g"] : key === "tier_kind" ? ["all_unit", "incremental"] : ["per_business", "collection_point"]).map((value) => <option key={value} value={value}>{text.options[value as keyof typeof text.options]}</option>)}
            </select></label>)}
            {(["pooled_buyers_permitted", "waive_split_drop_fees"] as const).map((key) => <label key={key} className="block text-sm">{labels[key]}<select className={inputClass} value={reading[key] === null || reading[key] === undefined ? "" : String(reading[key])} onChange={(event) => change(key, event.target.value === "" ? null : event.target.value === "true")}>
              <option value="">{text.unknown}</option><option value="true">{text.yes}</option><option value="false">{text.no}</option>
            </select></label>)}
            {(["valid_from", "valid_until", "arrival_start", "arrival_end"] as const).map((key) => <label key={key} className="block text-sm">{labels[key]}<Input type="datetime-local" step={1} value={reading[key] ? new Date(new Date(reading[key]).getTime() + 8 * 3600000).toISOString().slice(0, 19) : ""} onChange={(event) => change(key, event.target.value ? new Date(`${event.target.value}+08:00`).toISOString() : null)} /></label>)}
          </fieldset>
          <fieldset className="space-y-3 rounded-md border p-3" disabled={pending}>
            <legend className="px-1 text-sm font-semibold">{text.tiers}</legend>
            {reading.tiers?.map((tier, index) => <div key={index} className="grid gap-3 sm:grid-cols-2">
              <label className="block text-sm">{text.minimum}<Input required value={tier.min_quantity} onChange={(event) => change("tiers", reading.tiers?.map((item, i) => i === index ? { ...item, min_quantity: event.target.value } : item))} /></label>
              <label className="block text-sm">{text.unitPrice}<Input required value={tier.unit_price} onChange={(event) => change("tiers", reading.tiers?.map((item, i) => i === index ? { ...item, unit_price: event.target.value } : item))} /></label>
              <Button type="button" variant="outline" onClick={() => change("tiers", reading.tiers?.filter((_, i) => i !== index))}>{text.removeTier}</Button>
            </div>)}
            <Button type="button" variant="outline" disabled={(reading.tiers?.length ?? 0) >= 20} onClick={() => change("tiers", [...(reading.tiers ?? []), { min_quantity: "", unit_price: "" }])}>{text.addTier}</Button>
          </fieldset>
          <label className="flex items-start gap-3 text-sm"><input type="checkbox" className="mt-1 size-5 shrink-0" checked={reading.conditions_complete ?? false} onChange={(event) => change("conditions_complete", event.target.checked)} />{labels.conditions_complete}</label>
          <Button type="submit" variant="outline" disabled={pending || !dirty}>{text.save}</Button>
        </form>
        {dirty && <p>{text.saveFirst}</p>}
        <label className="block text-sm">{text.replace}<select className={inputClass} value={replace} onChange={(event) => { setReplace(event.target.value); setAcknowledged(false); }}>
          <option value="">{text.newQuote}</option>{quotes.filter((item) => item.supplier_id === proposal.context.supplier_id).map((item) => <option key={item.id} value={item.id}>{options?.suppliers.find((supplier) => supplier.id === item.supplier_id)?.name} · {text.revision} {item.revision} · {new Date(item.recorded_at).toLocaleDateString(locale)}</option>)}
        </select></label>
        <label className="flex items-start gap-3 text-sm"><input type="checkbox" className="mt-1 size-5 shrink-0" checked={acknowledged} disabled={dirty || pending} onChange={(event) => setAcknowledged(event.target.checked)} />{text.acknowledge}</label>
        <div className="flex flex-wrap gap-3"><Button disabled={pending || dirty || !acknowledged} onClick={() => void mutate("confirm")}>{text.confirm}</Button><Button variant="outline" disabled={pending} onClick={() => void mutate("reject")}>{text.reject}</Button></div>
      </> : <p className="text-sm">{reading.description ?? text.unknown}</p>}
      <details><summary className="cursor-pointer text-sm">{messages[locale].sourceReference}</summary><SourceViewer business={business} sourceId={source.id} locale={locale} evidence={proposal.evidence[0]} /></details>
    </>}
    <Button variant="outline" disabled={pending} onClick={() => { setError(""); setRefresh((value) => value + 1); }}>{text.refresh}</Button>
  </section>;
}
