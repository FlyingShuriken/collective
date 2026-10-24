"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";

import { DraftCard } from "@/components/draft-card";
import { SourceViewer } from "@/components/source-viewer";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError, apiRequest } from "@/lib/api/client";
import type { components } from "@/lib/api/generated";
import { money } from "@/lib/format";
import { messages, type Locale } from "@/lib/i18n";

type Check = components["schemas"]["VisibleInvoiceCheck"] | components["schemas"]["HiddenInvoiceCheck"];
type Page = components["schemas"]["InvoiceCheckPage"];
type Job = components["schemas"]["JobStatus"];
type Source = components["schemas"]["VisibleSource"];
const selectClass = "mt-2 min-h-11 w-full rounded-md border bg-card px-3 text-sm";

export function InvoiceCheck({ business, purchaseId, purchaseRevision, owner, locale, onChanged }: {
  business: string;
  purchaseId: string;
  purchaseRevision: number;
  owner: boolean;
  locale: Locale;
  onChanged: () => void;
}) {
  const text = messages[locale].invoices;
  const [page, setPage] = useState<Page | null>(null);
  const [current, setCurrent] = useState<Check | null>(null);
  const [orders, setOrders] = useState<components["schemas"]["InvoiceOrderOption"][]>([]);
  const [po, setPo] = useState("");
  const [job, setJob] = useState<Job | null>(null);
  const [draftJob, setDraftJob] = useState<Job | null>(null);
  const [draftId, setDraftId] = useState<string | null>(null);
  const [sources, setSources] = useState<Source[]>([]);
  const [sourceCursor, setSourceCursor] = useState<string | null>(null);
  const [proofId, setProofId] = useState("");
  const [reference, setReference] = useState("");
  const [resolution, setResolution] = useState<"credit_note" | "corrected_invoice">("credit_note");
  const [amount, setAmount] = useState("");
  const [recovery, setRecovery] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [documentReviewed, setDocumentReviewed] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [cursor, setCursor] = useState<string | null>(null);
  const wanted = useRef<string | null>(null);
  const retry = useRef<{ target: string; body: string; key: string } | null>(null);
  const root = `/v1/businesses/${business}` as const;
  const visible = current?.price_visibility === "visible";
  const proof = sources.find((source) => source.id === proofId);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function load() {
      try {
        const [checks, options] = await Promise.all([
          apiRequest<Page>(`${root}/invoice-checks?purchase_id=${purchaseId}&purchase_revision=${purchaseRevision}${cursor ? `&cursor=${cursor}` : ""}`, { signal: controller.signal }),
          apiRequest<components["schemas"]["InvoiceOrderOptions"]>(`${root}/invoice-checks/order-options?purchase_id=${purchaseId}&purchase_revision=${purchaseRevision}`, { signal: controller.signal }),
        ]);
        if (controller.signal.aborted) return;
        setPage(checks); setOrders(options.orders); setJob(checks.latest_job ?? null);
        const selected = checks.items.find((check) => check.id === wanted.current) ?? checks.items[0];
        if (selected) { if (!checks.latest_job || !["queued", "running"].includes(checks.latest_job.status)) { wanted.current = selected.id; setPo(selected.po_id ?? ""); } setCurrent(selected); }
        if (checks.latest_job && ["queued", "running"].includes(checks.latest_job.status)) timer = setTimeout(load, 1500);
      } catch { if (!controller.signal.aborted) setError(text.unavailable); }
    }
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [root, purchaseId, purchaseRevision, cursor, refresh, text.unavailable]);

  const checkId = current?.id;
  useEffect(() => {
    if (!checkId) return;
    const controller = new AbortController();
    apiRequest<components["schemas"]["DraftPage"]>(`${root}/drafts?reference_id=${checkId}&limit=1`, { signal: controller.signal })
      .then((value) => { if (!controller.signal.aborted) setDraftId(value.items[0]?.id ?? null); })
      .catch(() => { if (!controller.signal.aborted) setError(text.unavailable); });
    return () => controller.abort();
  }, [root, checkId, refresh, text.unavailable]);

  useEffect(() => {
    if (!draftJob || !["queued", "running"].includes(draftJob.status)) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      apiRequest<Job>(`${root}/jobs/${draftJob.id}`, { signal: controller.signal })
        .then((value) => { if (!controller.signal.aborted) { setDraftJob(value); if (value.result_reference) setDraftId(value.result_reference); } })
        .catch(() => { if (!controller.signal.aborted) setError(text.unavailable); });
    }, 1500);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [root, draftJob, text.unavailable]);

  useEffect(() => {
    if (!owner) return;
    const controller = new AbortController();
    apiRequest<components["schemas"]["SourcePageList"]>(`${root}/sources?limit=100`, { signal: controller.signal })
      .then((value) => { if (!controller.signal.aborted) { setSources(value.items.filter((source): source is Source => source.price_visibility === "visible" && source.preparation_status === "ready")); setSourceCursor(value.next_cursor); } })
      .catch(() => { if (!controller.signal.aborted) setError(text.unavailable); });
    return () => controller.abort();
  }, [root, owner, refresh, text.unavailable]);

  async function command<T>(target: `/v1/${string}`, value: object): Promise<T> {
    const body = JSON.stringify(value);
    if (retry.current?.target !== target || retry.current.body !== body) retry.current = { target, body, key: crypto.randomUUID() };
    return apiRequest<T>(target, { method: "POST", body, headers: { "Idempotency-Key": retry.current.key } });
  }

  function failed(error: unknown) { setError(error instanceof ApiError && error.detail.code === "RECOVERY_SOURCE_USED" ? text.evidenceUsed : error instanceof ApiError && error.status === 409 ? text.stale : error instanceof ApiError && error.status === 422 ? text.invalid : text.unavailable); }

  async function runCheck(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setPending(true); setError("");
    try {
      const value = await command<Job>(`${root}/invoice-checks`, { purchase_id: purchaseId, purchase_revision: purchaseRevision, po_id: po || null });
      wanted.current = value.result_reference ?? null;
      setJob(value); setCursor(null); setRefresh((value) => value + 1); onChanged();
    } catch (error) { failed(error); } finally { setPending(false); }
  }

  async function review(action: "confirm" | "reject" | "recover") {
    if (!visible) return;
    setPending(true); setError("");
    try {
      const value = await command<components["schemas"]["VisibleInvoiceCheck"]>(`${root}/invoice-checks/${current.id}/review`, {
        expected_revision: current.revision, action,
        ...(action === "confirm" ? { confirmed_amount: amount } : {}),
        ...(action === "recover" && proof?.sha256 ? { resolution: { kind: resolution, source_id: proof.id, source_sha256: proof.sha256, document_reference: reference, amount: recovery } } : {}),
      });
      setCurrent(value); setPage((page) => page ? { ...page, items: page.items.map((item) => item.id === value.id ? value : item) } : page); setAcknowledged(false); setDocumentReviewed(false); onChanged();
    } catch (error) { failed(error); } finally { setPending(false); }
  }

  async function requestQuery() {
    if (!visible) return;
    setPending(true); setError("");
    try {
      const value = await command<Job>(`${root}/drafts`, { kind: "invoice_query", reference_id: current.id, expected_revision: current.revision, supplier_id: current.supplier_id });
      setDraftJob(value); if (value.result_reference) setDraftId(value.result_reference); onChanged();
    } catch (error) { failed(error); } finally { setPending(false); }
  }

  async function moreSources() {
    if (!sourceCursor) return;
    setPending(true); setError("");
    try {
      const value = await apiRequest<components["schemas"]["SourcePageList"]>(`${root}/sources?limit=100&cursor=${sourceCursor}`);
      setSources((old) => [...old, ...value.items.filter((source): source is Source => source.price_visibility === "visible" && source.preparation_status === "ready")]); setSourceCursor(value.next_cursor);
    } catch (error) { failed(error); } finally { setPending(false); }
  }

  function field(path: string) {
    const parts = path.split(".");
    const labels: Record<string, string> = text.fields;
    return parts[0] === "lines" ? `${messages[locale].records.line} ${Number(parts[1]) + 1} · ${labels[parts[2]] ?? text.arithmetic}` : parts[0] === "additional_fees" ? messages[locale].records.additionalFees : labels[parts[0]] ?? text.arithmetic;
  }

  const result = visible ? current.result : null;
  return <section className="min-w-0 space-y-5 break-words border-t pt-5" aria-label={text.heading}>
    <h3 className="font-serif text-2xl">{text.heading}</h3>
    <p className="text-sm text-muted-foreground">{text.scope}</p>
    {error && <p role="alert" className="text-destructive">{error}</p>}
    {pending && <p role="status">{text.saving}</p>}
    {job && ["queued", "running"].includes(job.status) && <p role="status">{job.lease_expired ? messages[locale].leaseExpired : text.processing}</p>}
    {job?.status === "failed" && <p role="alert">{text.checkFailed}</p>}
    {!page && !error && <p role="status">{messages[locale].loading}</p>}
    {page?.items.length === 0 && <p className="text-sm">{text.noCheck}</p>}
    <form onSubmit={runCheck} className="space-y-3" aria-label={text.chooseOrder}>
      <label className="block text-sm">{text.chooseOrder}<select aria-label={text.chooseOrder} className={selectClass} value={po} disabled={pending} onChange={(event) => setPo(event.target.value)}>
        <option value="">{text.noOrder}</option>{orders.map((order) => <option key={order.id} value={order.id}>{money(order.amount)} · {order.quantity.replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "")} · {order.id.slice(0, 8)}</option>)}
      </select></label>
      {!orders.length && <p className="text-sm">{text.noOrders}</p>}
      <Button type="submit" disabled={pending}>{text.check}</Button>
    </form>
    {page && page.items.length > 1 && <label className="block text-sm">{text.previousChecks}<select aria-label={text.previousChecks} className={selectClass} value={current?.id ?? ""} onChange={(event) => {
      const selected = page.items.find((check) => check.id === event.target.value);
      if (selected) { wanted.current = selected.id; setCurrent(selected); setPo(selected.po_id ?? ""); setAcknowledged(false); setDocumentReviewed(false); setAmount(""); setRecovery(""); setProofId(""); setReference(""); setDraftJob(null); setDraftId(null); }
    }}>{page.items.map((check) => <option key={check.id} value={check.id}>{text.status[check.status]} · {check.po_id?.slice(0, 8) ?? text.noOrder}</option>)}</select></label>}
    {page?.next_cursor && <Button variant="outline" disabled={pending} onClick={() => { wanted.current = null; setCursor(page.next_cursor ?? null); }}>{messages[locale].nextPage}</Button>}
    {current && <p role="status" className="text-sm font-semibold">{text.status[current.status]}</p>}
    {current?.po_id && <p className="break-all text-xs text-muted-foreground">{messages[locale].orderReference}: {current.po_id}</p>}
    {current?.price_visibility === "hidden" && <p>{messages[locale].pricesHidden}</p>}
    {visible && result && <>
      <div className="rounded-lg bg-muted p-4">
        <p className="text-sm">{text.totalVariance}</p><p className="mt-1 text-2xl font-semibold tabular-nums">{money(result.total_variance)}</p>
        <p className="mt-2 text-xs text-muted-foreground">{text.notAdditive}</p>
      </div>
      {!result.complete && <p className="text-sm">{text.incomplete}</p>}
      {result.failure_codes.length > 0 && <p className="text-sm">{text.unmatchedHelp}</p>}
      {result.missing_fields.length > 0 && <ul className="space-y-1 text-sm">{result.missing_fields.map((name) => <li key={name}>{field(name)} · {messages[locale].records.unknown}</li>)}</ul>}
      <ol className="space-y-3" aria-label={text.differences}>{result.differences.map((difference, index) => <li key={`${difference.field_path}:${index}`} className="space-y-2 rounded-lg border p-3 text-sm">
        <p className="font-semibold">{field(difference.field_path)}</p>
        <dl className="grid grid-cols-2 gap-2 tabular-nums"><div><dt>{text.agreed}</dt><dd>{difference.agreed}</dd></div><div><dt>{text.observed}</dt><dd>{difference.invoiced}</dd></div><div><dt>{text.delta}</dt><dd>{difference.delta}</dd></div><div><dt>{text.cashEffect}</dt><dd>{money(difference.amount_difference)}</dd></div></dl>
        <p className="text-xs text-muted-foreground">{difference.material ? text.material : text.belowThreshold}</p>
      </li>)}</ol>
      <dl className="grid gap-3 text-sm sm:grid-cols-2"><div><dt>{text.confirmedValue}</dt><dd>{money(current.confirmed_amount)}</dd></div><div><dt>{text.recoveredValue}</dt><dd>{current.recovery_amount == null ? text.notRecovered : money(current.recovery_amount)}</dd></div></dl>
      {current.review_status === "rejected" && <p role="status">{text.rejected}</p>}
      {current.review_status === "confirmed" && <p role="status">{current.recovery_amount == null ? text.confirmed : text.recovered}</p>}
      {current.status === "flagged" && owner && current.review_status === "pending" && <form className="space-y-3" aria-label={text.reviewDiscrepancy} onSubmit={(event) => { event.preventDefault(); void review("confirm"); }}>
        <label className="block text-sm">{text.confirmedValue}<Input className="mt-2" inputMode="decimal" pattern="[0-9]+([.][0-9]{1,2})?" required value={amount} disabled={pending} onChange={(event) => { setAmount(event.target.value); setAcknowledged(false); }} /></label>
        <p className="text-sm text-muted-foreground">{text.confirmAmountScope}</p>
        <label className="flex items-start gap-3 text-sm"><input type="checkbox" className="mt-1 size-5 shrink-0" checked={acknowledged} disabled={pending} onChange={(event) => setAcknowledged(event.target.checked)} />{text.reviewedDifference}</label>
        <div className="flex flex-wrap gap-3"><Button type="submit" disabled={pending || !acknowledged}>{text.confirm}</Button><Button type="button" variant="outline" disabled={pending} onClick={() => void review("reject")}>{text.reject}</Button></div>
      </form>}
      {current.status === "flagged" && owner && !draftId && <>
        {draftJob && ["queued", "running"].includes(draftJob.status) && <p role="status">{text.drafting}</p>}
        {draftJob?.status === "failed" && <p role="alert">{draftJob.error_code === "REPLAY_MISS" ? text.replayMiss : text.queryFailed}</p>}
        <Button type="button" variant="outline" disabled={pending || !!draftJob && ["queued", "running"].includes(draftJob.status)} onClick={() => void requestQuery()}>{text.requestQuery}</Button>
      </>}
      {!owner && current.status === "flagged" && <p>{text.ownerOnly}</p>}
      {draftId && <DraftCard key={draftId} business={business} draftId={draftId} locale={locale} owner={owner} onChanged={onChanged} />}
      {current.review_status === "confirmed" && current.recovery_amount == null && owner && <section className="space-y-3" aria-label={text.resolution}>
        <h4 className="font-semibold">{text.resolution}</h4><p className="text-sm">{text.proofScope}</p>
        <label className="block text-sm">{text.proofSource}<select aria-label={text.proofSource} className={selectClass} value={proofId} disabled={pending} onChange={(event) => { setProofId(event.target.value); setDocumentReviewed(false); }}>
          <option value="">{text.chooseSource}</option>{sources.filter((source) => source.id !== current.source_id).map((source) => <option key={source.id} value={source.id}>{source.kind} · {source.id}</option>)}
        </select></label>
        {sourceCursor && <Button variant="outline" disabled={pending} onClick={() => void moreSources()}>{text.moreSources}</Button>}
        {proof && <SourceViewer key={proof.id} business={business} sourceId={proof.id} locale={locale} />}
        <form className="space-y-3" aria-label={text.recordRecovery} onSubmit={(event) => { event.preventDefault(); void review("recover"); }}>
          <label className="block text-sm">{text.resolutionType}<select aria-label={text.resolutionType} className={selectClass} value={resolution} disabled={pending} onChange={(event) => { setResolution(event.target.value as typeof resolution); setDocumentReviewed(false); }}><option value="credit_note">{text.creditNote}</option><option value="corrected_invoice">{text.correctedInvoice}</option></select></label>
          <label className="block text-sm">{text.documentReference}<Input className="mt-2" required maxLength={300} value={reference} disabled={pending} onChange={(event) => { setReference(event.target.value); setDocumentReviewed(false); }} /></label>
          <label className="block text-sm">{text.recoveryAmount}<Input className="mt-2" inputMode="decimal" pattern="[0-9]+([.][0-9]{1,2})?" required value={recovery} disabled={pending} onChange={(event) => { setRecovery(event.target.value); setDocumentReviewed(false); }} /></label>
          <label className="flex items-start gap-3 text-sm"><input type="checkbox" className="mt-1 size-5 shrink-0" checked={documentReviewed} disabled={pending || !proof} onChange={(event) => setDocumentReviewed(event.target.checked)} />{text.reviewedProof}</label>
          <Button type="submit" disabled={pending || !proof?.sha256 || !documentReviewed}>{text.recordRecovery}</Button>
        </form>
      </section>}
      {current.resolution && <p className="break-words text-sm">{text.documentReference}: {current.resolution.document_reference} · {money(current.resolution.amount)}</p>}
    </>}
    <Button type="button" variant="outline" disabled={pending} onClick={() => { setError(""); setRefresh((value) => value + 1); }}>{text.refresh}</Button>
  </section>;
}
