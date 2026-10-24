"use client";

import { useEffect, useState } from "react";

import { SourceViewer } from "@/components/source-viewer";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError, apiRequest } from "@/lib/api/client";
import type { components } from "@/lib/api/generated";
import { messages, type Locale } from "@/lib/i18n";

type Mapping = components["schemas"]["MappingView"];
type Job = components["schemas"]["JobStatus"];

export function SpecMatchCard({ business, sourceId, supplierId, description, sku, locale, onConfirmed }: {
  business: string; sourceId: string; supplierId: string; description: string; sku: string;
  locale: Locale; onConfirmed: () => void;
}) {
  const text = messages[locale].matching;
  const [items, setItems] = useState<Mapping[]>([]);
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const root = `/v1/businesses/${business}` as const;

  async function load() {
    const result = await apiRequest<components["schemas"]["MappingPage"]>(`${root}/mappings`);
    setItems(result.items.filter((item) => item.source_id === sourceId));
  }

  useEffect(() => {
    const controller = new AbortController();
    apiRequest<components["schemas"]["MappingPage"]>(`${root}/mappings`, { signal: controller.signal })
      .then((value) => { if (!controller.signal.aborted) setItems(value.items.filter((item) => item.source_id === sourceId)); })
      .catch(() => { if (!controller.signal.aborted) setError(text.failed); });
    return () => controller.abort();
  }, [root, sourceId, text.failed]);

  useEffect(() => {
    if (!job || ["succeeded", "failed"].includes(job.status)) return;
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const next = await apiRequest<Job>(`${root}/jobs/${job.id}`, { signal: controller.signal });
        const result = await apiRequest<components["schemas"]["MappingPage"]>(`${root}/mappings`, { signal: controller.signal });
        if (!controller.signal.aborted) {
          setJob(next); setItems(result.items.filter((item) => item.source_id === sourceId));
        }
      } catch { if (!controller.signal.aborted) setError(text.failed); }
    }, 1500);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [job, root, sourceId, text.failed]);

  async function request(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); setPending(true); setError("");
    const fields = new FormData(event.currentTarget);
    try {
      const source = await apiRequest<components["schemas"]["SourceResult"]>(`${root}/sources/${sourceId}`);
      if (source.source.price_visibility !== "visible" || !source.source.sha256) throw new Error();
      setJob(await apiRequest<Job>(`${root}/mappings`, {
        method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ source_id: sourceId, source_sha256: source.source.sha256,
          supplier_id: supplierId, supplier_sku: fields.get("sku"), source_name: fields.get("description") }),
      }));
      await load();
    } catch { setError(text.failed); } finally { setPending(false); }
  }

  return <section className="space-y-4 py-4" aria-label={text.title}>
    <p className="text-sm text-muted-foreground">{text.explanation}</p>
    <form className="space-y-3" onSubmit={(event) => void request(event)}>
      <label className="block text-sm">{text.description}<Input name="description" required maxLength={1000} defaultValue={description} /></label>
      <label className="block text-sm">{text.sku}<Input name="sku" required maxLength={200} defaultValue={sku} /></label>
      <Button type="submit" disabled={pending || !!job && !["failed", "succeeded"].includes(job.status)}>{pending ? text.working : text.request}</Button>
    </form>
    {error && <p role="alert" className="text-destructive">{error}</p>}
    {job?.status === "failed" && <p role="status">{text.manual}</p>}
    {job && !["failed", "succeeded"].includes(job.status) && <p role="status">{text.working}</p>}
    {items.map((item) => <MappingReview key={`${item.id}:${item.revision}`} mapping={item} business={business} locale={locale} onConfirmed={() => { void load(); onConfirmed(); }} />)}
  </section>;
}

function MappingReview({ mapping, business, locale, onConfirmed }: {
  mapping: Mapping; business: string; locale: Locale; onConfirmed: () => void;
}) {
  const text = messages[locale].matching;
  const candidates = (Array.isArray(mapping.match_snapshot.candidates) ? mapping.match_snapshot.candidates : []) as { key: string; text: string }[];
  const [spec, setSpec] = useState(mapping.spec_id ? `${mapping.spec_id}:${mapping.spec_revision}` : "");
  const [unit, setUnit] = useState(mapping.source_unit ?? "piece");
  const [factors, setFactors] = useState(mapping.pack_factors.join(" × "));
  const [reviewed, setReviewed] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const selectClass = "mt-2 min-h-11 w-full rounded-md border bg-card px-3";

  async function confirm(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); setPending(true); setError("");
    const [specId, revision] = spec.split(":");
    try {
      await apiRequest<Mapping>(`/v1/businesses/${business}/mappings/${mapping.id}/confirm`, {
        method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ expected_revision: mapping.revision, spec_id: specId,
          spec_revision: Number(revision), source_unit: unit,
          pack_factors: factors.split(/[×x*,]/).map((value) => value.trim()), acknowledge_flags: reviewed }),
      });
      onConfirmed();
    } catch (error) { setError(error instanceof ApiError && error.status === 409 ? text.stale : text.failed); }
    finally { setPending(false); }
  }

  return <section className="space-y-3 rounded-md border p-3">
    <h4 className="font-semibold">{mapping.supplier_sku} · {mapping.source_name}</h4>
    {mapping.confirmed ? <p role="status">{text.confirmed}</p> : <form className="space-y-3" onSubmit={(event) => void confirm(event)}>
      <p className="text-sm">{text.review}</p>
      <label className="block text-sm">{text.spec}<select required className={selectClass} value={spec} onChange={(event) => setSpec(event.target.value)}>
        <option value="">{text.select}</option>
        {candidates.map((candidate) => <option key={candidate.key} value={candidate.key}>{candidate.text}</option>)}
      </select></label>
      <label className="block text-sm">{text.unit}<select className={selectClass} value={unit} onChange={(event) => setUnit(event.target.value)}>
        {(["piece", "box", "carton", "pack"] as const).map((value) => <option key={value} value={value}>{text.units[value]}</option>)}
      </select></label>
      <label className="block text-sm">{text.factors}<Input required value={factors} onChange={(event) => setFactors(event.target.value)} placeholder="20 × 50" /></label>
      <label className="flex items-start gap-3 text-sm"><input type="checkbox" className="mt-1 size-5 shrink-0" checked={reviewed} onChange={(event) => setReviewed(event.target.checked)} />{text.acknowledge}</label>
      <Button type="submit" disabled={pending || !reviewed || !spec}>{text.confirm}</Button>
      {error && <p role="alert" className="text-destructive">{error}</p>}
    </form>}
    {mapping.source_id && <details><summary className="cursor-pointer text-sm">{messages[locale].sourceReference}</summary><SourceViewer business={business} sourceId={mapping.source_id} locale={locale} /></details>}
  </section>;
}
