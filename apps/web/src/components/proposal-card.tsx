"use client";

import { useEffect, useRef, useState } from "react";

import { ProposalEditor } from "@/components/proposal-editor";
import { SourceViewer } from "@/components/source-viewer";
import { Button } from "@/components/ui/button";
import { ApiError, apiRequest } from "@/lib/api/client";
import type { components } from "@/lib/api/generated";
import { money } from "@/lib/format";
import { messages, type Locale } from "@/lib/i18n";

type Proposal = components["schemas"]["DocumentProposal"];
type AnyProposal = Proposal | components["schemas"]["QuoteProposal"] | components["schemas"]["HiddenProposal"];
type Source = components["schemas"]["SourceResult"];

function fieldLabel(path: string, locale: Locale) {
  const text = messages[locale].records;
  const fields: Record<string, string> = { ...text.fields, supplier: text.confirmedSupplier, purpose: text.purpose,
    source: messages[locale].sourceReference, reading: text.reading, fields: text.reading,
    evidence: text.evidence, stronger_reading: text.reading, record_kind: text.recordType,
    mapping: text.mapping, stock_mapping: text.mapping, normalization: text.mapping, stock_value: text.reviewValue,
  };
  const parts = path.split(".");
  if (parts[0] === "lines") return parts[1] === undefined ? text.lines : `${text.line} ${Number(parts[1]) + 1} · ${fields[parts[2] === "total" ? "line_total" : parts[2]] ?? text.lines}`;
  if (parts[0] === "additional_fees") return text.additionalFees;
  return fields[parts[0]] ?? text.reading;
}

export function ProposalCard({ business, proposal, locale, onChanged, onRefresh }: {
  business: string;
  proposal: Proposal;
  locale: Locale;
  onChanged: (proposal: Proposal) => void;
  onRefresh: () => void;
}) {
  const text = messages[locale].records;
  const [editing, setEditing] = useState(false);
  const [options, setOptions] = useState<components["schemas"]["RecordOptions"] | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [evidence, setEvidence] = useState<number | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const retry = useRef<{ path: string; body: string; key: string } | null>(null);
  const path = `/v1/businesses/${business}/proposals/${proposal.id}` as const;

  useEffect(() => {
    const controller = new AbortController();
    apiRequest<components["schemas"]["RecordOptions"]>(`/v1/businesses/${business}/record-options`, { signal: controller.signal })
      .then((value) => { if (!controller.signal.aborted) setOptions(value); })
      .catch(() => { if (!controller.signal.aborted) setError(text.reviewUnavailable); });
    return () => controller.abort();
  }, [business, text.reviewUnavailable]);

  async function mutate(action: "edit" | "confirm" | "reject", value: object) {
    setPending(true); setError("");
    const target = action === "edit" ? path : `${path}/${action}` as const;
    const body = JSON.stringify(value);
    if (retry.current?.path !== target || retry.current?.body !== body) retry.current = { path: target, body, key: crypto.randomUUID() };
    try {
      onChanged(await apiRequest<Proposal>(target, {
        method: action === "edit" ? "PATCH" : "POST", headers: { "Idempotency-Key": retry.current.key }, body,
      }));
    } catch (error) {
      setError(error instanceof ApiError && error.detail.code === "OWNER_REVIEW_REQUIRED" ? text.ownerRequired
        : error instanceof ApiError && error.status === 409 ? text.stale
        : error instanceof ApiError && error.status === 422 ? text.invalid : text.reviewUnavailable);
    } finally { setPending(false); }
  }

  const flags = proposal.flags;
  const reasons: Record<string, string> = {
    missing: text.unknown, ambiguous: text.ambiguous, unsupported: text.unsupported,
    mismatch: text.mismatch, overflow: text.outsideRange, negative: text.outsideRange,
    comparison_basis_unknown: text.comparisonUnknown, review: text.needsReview,
    failed: text.needsReview, stale: text.staleMapping, context_mismatch: text.staleMapping,
    incomplete: text.incomplete, normalization_required: text.incomplete, unreadable: text.unreadable,
  };
  const purchase = proposal.kind === "purchase";
  const reading = proposal.reading;
  return <section className="min-w-0 space-y-5 break-words rounded-xl border border-primary/30 bg-card p-4 sm:p-6" aria-label={text.review}>
    <header className="flex flex-wrap items-start justify-between gap-3 border-b pb-4">
      <div><p className="text-xs font-semibold uppercase tracking-widest">{text.gateOne}</p>
        <h3 className="mt-2 font-serif text-2xl">{purchase ? text.purchase : text.stock}</h3>
        <p role="status" className="mt-2 text-sm font-semibold">{text.status[proposal.status]}</p>
      </div>
      <p className="text-xs text-muted-foreground">{text.revision} {proposal.current_revision}</p>
    </header>
    <p className="text-sm text-muted-foreground">{proposal.interpretation_mode === "replay" ? text.replayReading : text.aiReading}</p>
    {error && <p role="alert" className="text-destructive">{error}</p>}
    {pending && <p role="status">{text.saving}</p>}
    {!editing && <>
      <dl className="grid gap-3 text-sm sm:grid-cols-2">
        {purchase ? <>
          <div><dt className="text-muted-foreground">{text.fields.supplier_name}</dt><dd>{reading.supplier_name ?? text.unknown}</dd></div>
          <div><dt className="text-muted-foreground">{text.confirmedSupplier}</dt><dd>{options?.suppliers.find((supplier) => supplier.id === proposal.context.supplier_id)?.name ?? text.unknown}</dd></div>
          <div><dt className="text-muted-foreground">{text.fields.document_date}</dt><dd>{reading.document_date ?? text.unknown}</dd></div>
          <div><dt className="text-muted-foreground">{text.fields.total}</dt><dd className="font-semibold tabular-nums">{reading.currency ?? text.unknown} {reading.total ?? "—"}</dd></div>
          <div><dt className="text-muted-foreground">{text.calculatedGoods}</dt><dd className="tabular-nums">{reading.currency ?? text.unknown} {proposal.checks.calculated_goods ?? "—"}</dd></div>
          <div><dt className="text-muted-foreground">{text.calculatedTotal}</dt><dd className="tabular-nums">{reading.currency ?? text.unknown} {proposal.checks.calculated_total ?? "—"}</dd></div>
          <div><dt className="text-muted-foreground">{text.purpose}</dt><dd>{proposal.context.purpose ? text[proposal.context.purpose] : text.unknown}</dd></div>
        </> : <>
          <div><dt className="text-muted-foreground">{text.stockKind}</dt><dd>{({ stock: text.stockCount, receipt: text.receipt, inbound_adjustment: text.inbound, waste: text.waste })[proposal.context.stock_kind ?? "stock"]}</dd></div>
          <div><dt className="text-muted-foreground">{text.fields.stock_description}</dt><dd>{reading.stock_description ?? text.unknown}</dd></div>
          <div><dt className="text-muted-foreground">{text.fields.stock_quantity}</dt><dd>{reading.stock_quantity ?? "—"} {reading.stock_unit ?? text.unknown}</dd></div>
          <div><dt className="text-muted-foreground">{text.fields.stock_observed_at}</dt><dd>{reading.stock_observed_at ? new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Kuala_Lumpur" }).format(new Date(reading.stock_observed_at)) : text.unknown}</dd></div>
        </>}
        <div><dt className="text-muted-foreground">{text.reviewValue}</dt><dd>{(!purchase || reading.currency === "MYR") && proposal.review_value != null ? money(proposal.review_value) : text.unknown}</dd></div>
      </dl>
      {purchase && <ol className="space-y-2 text-sm">{reading.lines?.map((line, index) => <li key={index} className="rounded border p-3">
        <p className="font-semibold">{text.line} {index + 1} · {line.description ?? text.unknown}</p>
        <p className="mt-1 tabular-nums">{line.quantity ?? "—"} {line.unit ?? text.unknown} × {line.unit_price ?? "—"} = {line.line_total ?? "—"}</p>
        <p className="mt-1 text-xs text-muted-foreground">{(() => {
          const selected = proposal.context.line_mappings?.find((mapping) => mapping.line === index);
          const mapping = options?.mappings.find((mapping) => mapping.id === selected?.id && mapping.revision === selected.revision);
          return mapping ? `${mapping.supplier_sku} · ${mapping.spec_name}` : selected ? text.mapped : text.unmapped;
        })()}</p>
      </li>)}</ol>}
      {purchase && <details className="space-y-3 rounded border p-3">
        <summary className="cursor-pointer text-sm font-semibold">{text.charges}</summary>
        <dl className="mt-3 grid gap-3 text-sm sm:grid-cols-2">{(["document_number", "goods_total", "delivery_fee", "tax_amount", "tax_rate", "discount", "surcharge", "credit_days", "delivery_mode"] as const).map((key) => <div key={key}>
          <dt className="text-muted-foreground">{text.fields[key]}</dt><dd>{key === "delivery_mode" ? reading.delivery_mode === "per_business" ? text.perBusiness : reading.delivery_mode === "collection_point" ? text.collectionPoint : text.unknown : reading[key] ?? text.unknown}</dd>
        </div>)}</dl>
        {reading.additional_fees?.map((fee, index) => <p key={index} className="text-sm">{fee.label ?? text.unknown}: {fee.amount ?? text.unknown}</p>)}
      </details>}
    </>}
    <section className="space-y-3" aria-label={text.evidence}>
      <h4 className="text-sm font-semibold">{text.evidence}</h4>
      <p className="text-xs text-muted-foreground">{text.originalEvidence}</p>
      <div className="flex flex-wrap gap-2">{proposal.evidence.map((anchor, index) => <Button key={index} type="button" variant="outline" className="h-auto min-h-11 max-w-full whitespace-normal text-left" aria-pressed={evidence === index} onClick={() => setEvidence(evidence === index ? null : index)}>
        {fieldLabel(anchor.field_path, locale)} · {anchor.extracted_value ?? text.fullPage}
      </Button>)}</div>
      {evidence !== null && <SourceViewer key={`${proposal.id}:${evidence}`} business={business} sourceId={proposal.source_id} locale={locale} evidence={proposal.evidence[evidence]} />}
    </section>
    {editing ? options ? <ProposalEditor proposal={proposal} options={options} locale={locale} pending={pending} onSave={(body) => void mutate("edit", body)} onCancel={() => { setEditing(false); setError(""); }} /> : <p role="status">{messages[locale].loading}</p> : <>
      <section className="space-y-2 border-t pt-4" aria-label={text.flags}>
        <h4 className="text-sm font-semibold">{text.flags}</h4>
        {flags.length ? <ul className="space-y-1 text-sm">{flags.map((flag) => {
          const [field, reason] = flag.split(":");
          return <li key={flag}>{fieldLabel(field, locale)} · {reasons[reason] ?? text.needsReview}</li>;
        })}</ul> : <p className="text-sm">{text.noFlags}</p>}
      </section>
      {proposal.status === "pending_review" ? <>
        <p className="text-sm">{purchase ? text.invoiceScope : text.stockScope}</p>
        <p className="text-sm text-muted-foreground">{text.staffScope}</p>
        <label className="flex items-start gap-3 text-sm"><input type="checkbox" className="mt-1 size-5 shrink-0" checked={acknowledged} disabled={pending} onChange={(event) => setAcknowledged(event.target.checked)} />{text.acknowledge}</label>
        <div className="flex flex-wrap gap-3">
          <Button type="button" variant="outline" disabled={pending} onClick={() => { setEditing(true); setError(""); }}>{text.editRecord}</Button>
          <Button type="button" disabled={pending || !acknowledged} onClick={() => void mutate("confirm", { expected_revision: proposal.current_revision, acknowledged_flags: flags })}>{text.confirm}</Button>
          <Button type="button" variant="outline" disabled={pending} onClick={() => void mutate("reject", { expected_revision: proposal.current_revision })}>{text.reject}</Button>
        </div>
      </> : <p className="text-sm">{proposal.status === "confirmed" ? text.confirmedScope : text.rejectedScope}</p>}
    </>}
    {proposal.record_id && <p className="break-all text-xs text-muted-foreground">{text.recordReference}: {proposal.record_id}</p>}
    <Button type="button" variant="outline" disabled={pending} onClick={onRefresh}>{text.refresh}</Button>
  </section>;
}

export function SourceIntake({ business, sourceId, locale, onChanged }: {
  business: string;
  sourceId: string;
  locale: Locale;
  onChanged: () => void;
}) {
  const text = messages[locale].records;
  const [result, setResult] = useState<Source | null>(null);
  const [proposal, setProposal] = useState<AnyProposal | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const retryKey = useRef<string | null>(null);
  const root = `/v1/businesses/${business}/sources/${sourceId}` as const;

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function load() {
      try {
        const value = await apiRequest<Source>(root, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setResult(value);
        if (value.source.price_visibility !== "visible") return;
        if (value.job?.result_reference) {
          const current = await apiRequest<AnyProposal>(`/v1/businesses/${business}/proposals/${value.job.result_reference}`, { signal: controller.signal });
          if (!controller.signal.aborted) setProposal(current);
        }
        if (!controller.signal.aborted && value.job && ["queued", "running"].includes(value.job.status)) timer = setTimeout(load, 2000);
      } catch { if (!controller.signal.aborted) setError(text.reviewUnavailable); }
    }
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [root, business, refresh, text.reviewUnavailable]);

  function reload() { setError(""); setProposal(null); setRefresh((value) => value + 1); }

  async function retry() {
    if (result?.source.price_visibility !== "visible" || !result.source.sha256) return;
    retryKey.current ??= crypto.randomUUID();
    setPending(true); setError("");
    try {
      await apiRequest(`${root}/intake`, { method: "POST", headers: { "Idempotency-Key": retryKey.current }, body: JSON.stringify({ source_sha256: result.source.sha256 }) });
      retryKey.current = null;
      reload(); onChanged();
    } catch { setError(text.reviewUnavailable); }
    finally { setPending(false); }
  }

  if (!result) return error ? <p role="alert" className="text-destructive">{error}</p> : null;
  if (result.source.price_visibility === "hidden") return null;
  if (result?.source.preparation_status !== "ready") return null;
  if (proposal?.price_visibility === "visible" && proposal.kind !== "quote") return <ProposalCard key={`${proposal.id}:${proposal.current_revision}`} business={business} proposal={proposal} locale={locale} onRefresh={reload} onChanged={(value) => { setProposal(value); onChanged(); }} />;
  return <section className="space-y-3 rounded-lg border p-4" aria-label={text.reading}>
    <h3 className="font-semibold">{text.reading}</h3>
    {error && <p role="alert" className="text-destructive">{error}</p>}
    <p role="status" className="text-sm">{result.job?.status === "failed" ? result.job.error_code === "REPLAY_MISS" ? text.replayMiss : text.readingFailed
      : result.job?.review_state === "needs_quote_context" || proposal?.kind === "quote" ? text.quoteContext
      : result.job?.status === "succeeded" ? text.manualReview : text.readingPending}</p>
    <div className="flex flex-wrap gap-3">
      {result.job?.status === "failed" && <Button type="button" disabled={pending} onClick={() => void retry()}>{text.retryReading}</Button>}
      <Button type="button" variant="outline" disabled={pending} onClick={reload}>{text.refresh}</Button>
    </div>
  </section>;
}
