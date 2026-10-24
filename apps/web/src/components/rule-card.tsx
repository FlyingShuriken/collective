"use client";

import { useEffect, useRef, useState } from "react";

import { SourceViewer } from "@/components/source-viewer";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ApiError, apiRequest } from "@/lib/api/client";
import type { components } from "@/lib/api/generated";
import { date as formatDate, decimal, money } from "@/lib/format";
import { messages, type Locale } from "@/lib/i18n";

type Rule = components["schemas"]["RulePage"]["items"][number];
type Page = components["schemas"]["RulePage"];
type Job = components["schemas"]["JobStatus"];
type Action = "accept" | "reject" | "revoke";

function date(value: string, locale: Locale) {
  return formatDate(value, locale);
}

export function AccuracyNotice({ warning, locale }: { warning: components["schemas"]["AccuracyWarning"]; locale: Locale }) {
  const text = messages[locale].rules;
  return <aside className="mt-3 space-y-1 rounded-md bg-muted p-3 text-sm" aria-label={text.accuracyWarning}>
    <p className="font-medium">{text.accuracyWarning}</p>
    <p>{text.mismatches}: {warning.mismatch_count} / {warning.checked_count} · {text.checked}</p>
    <p>{date(warning.period_start, locale)} – {date(warning.period_end, locale)}</p>
    <p className="text-muted-foreground">{text.accuracyLimit}</p>
  </aside>;
}

export function RuleList({ scope, scopeId, locale, canReview, refresh, onChanged }: {
  scope: "private" | "network";
  scopeId: string;
  locale: Locale;
  canReview: boolean;
  refresh: number;
  onChanged: () => void;
}) {
  const text = messages[locale].rules;
  const [page, setPage] = useState<Page | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const retry = useRef<string | null>(null);
  const root = `/v1/${scope === "private" ? "businesses" : "networks"}/${scopeId}/rules` as const;
  const active = !!page?.latest_job && ["queued", "running"].includes(page.latest_job.status);
  const heading = scope === "private" ? text.heading : text.networkHeading;

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function load() {
      try {
        const value = await apiRequest<Page>(`${root}${cursor ? `?cursor=${cursor}` : ""}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setPage(value);
        if (value.latest_job && ["queued", "running"].includes(value.latest_job.status)) timer = setTimeout(load, 1000);
      } catch {
        if (!controller.signal.aborted) setError(text.unavailable);
      }
    }
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [root, cursor, revision, refresh, text.unavailable]);

  async function generate() {
    setPending(true); setError("");
    retry.current ??= crypto.randomUUID();
    try {
      const job = await apiRequest<Job>(`${root}/evaluate`, { method: "POST", headers: { "Idempotency-Key": retry.current } });
      retry.current = null;
      setPage((old) => old ? { ...old, latest_job: job } : old);
      setCursor(null); setRevision((value) => value + 1);
    } catch { setError(text.unavailable); } finally { setPending(false); }
  }

  return <section className="min-w-0 space-y-5 border-t pt-6" aria-label={heading}>
    <h2 className="font-serif text-2xl">{heading}</h2>
    <p className="text-sm text-muted-foreground">{scope === "private" ? text.privateScope : text.networkScope}</p>
    {scope === "network" && <p className="text-sm">{text.networkMinimum}</p>}
    {error && <p role="alert" className="text-destructive">{error}</p>}
    {!page && !error && <p role="status">{messages[locale].loading}</p>}
    {active && <p role="status">{page?.latest_job?.lease_expired ? messages[locale].leaseExpired : text.processing}</p>}
    {page?.latest_job?.status === "failed" && <p role="alert">{text.failed}</p>}
    {page?.items.length === 0 && !active && <p>{scope === "private" ? text.empty : text.networkEmpty}</p>}
    <div className="flex flex-wrap gap-3">
      {canReview && <Button variant="outline" disabled={pending || active} onClick={generate}>{text.generate}</Button>}
      <Button variant="outline" disabled={pending} onClick={() => { setCursor(null); setError(""); setRevision((value) => value + 1); }}>{text.refresh}</Button>
    </div>
    {page?.items.map((rule) => <RuleCard key={`${rule.id}:${rule.revision}`} rule={rule} root={root} locale={locale} canReview={canReview} onChanged={(changed) => {
      setPage((old) => old ? { ...old, items: old.items.map((item) => item.id === changed.id ? changed : item) } : old);
      setRevision((value) => value + 1); onChanged();
    }} />)}
    {page?.next_cursor && <Button variant="outline" onClick={() => { setCursor(page.next_cursor ?? null); setPage(null); }}>{messages[locale].nextPage}</Button>}
  </section>;
}

function RuleCard({ rule, root, locale, canReview, onChanged }: {
  rule: Rule;
  root: `/v1/${string}`;
  locale: Locale;
  canReview: boolean;
  onChanged: (rule: Rule) => void;
}) {
  const text = messages[locale].rules;
  const [acknowledged, setAcknowledged] = useState(false);
  const [source, setSource] = useState<components["schemas"]["EvidenceReference"] | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const visible = rule.price_visibility === "visible";
  const heading = text.kinds[rule.kind];
  const uncertainty: Record<string, string> = text.uncertainty;

  async function act(action: Action) {
    setPending(true); setError("");
    try {
      const changed = await apiRequest<Rule>(`${root}/${rule.id}/${action}`, { method: "POST", body: JSON.stringify({ expected_revision: rule.revision }) });
      onChanged(changed);
    } catch (error) {
      setError(error instanceof ApiError && error.detail.code === "INSUFFICIENT_EVIDENCE" ? text.insufficient : error instanceof ApiError && error.status === 409 ? text.stale : text.unavailable);
    } finally { setPending(false); }
  }

  return <article aria-label={heading} className="min-w-0">
    <Card>
      <CardHeader>
        <CardTitle><h3>{heading}</h3></CardTitle>
        {visible && <p className="font-medium">{rule.context_name ?? text.unknownContext}</p>}
        <p role="status" className="text-sm">{text.states[rule.state]}</p>
      </CardHeader>
      <CardContent className="min-w-0 space-y-4 break-words">
        {error && <p role="alert" className="text-destructive">{error}</p>}
        <dl className="grid grid-cols-2 gap-3 text-sm">
          <div><dt>{text.samples}</dt><dd className="font-medium">{rule.sample_count}</dd></div>
          {rule.scope === "network" && <div><dt>{text.businesses}</dt><dd className="font-medium">{rule.distinct_businesses}</dd></div>}
          <div className="col-span-2"><dt>{text.period}</dt><dd>{date(rule.period_start, locale)} – {date(rule.period_end, locale)}</dd></div>
          <div><dt>{text.excluded}</dt><dd>{rule.excluded_count}</dd></div>
        </dl>
        {!visible && <p>{messages[locale].pricesHidden}</p>}
        {visible && <>
          {rule.value.kind === "observed_fee" && <div className="space-y-2 text-sm">
            <p className="text-xl font-semibold">{text.meanFee}: <span className="whitespace-nowrap">{money(rule.value.amount)}</span></p>
            <p>{text.range}: {money(rule.value.minimum)} – {money(rule.value.maximum)}</p>
            {"zone" in rule.context && <p>{text.zone}: {rule.context.zone} · {text.modes[rule.context.delivery_mode]}{rule.context.delivery_location ? ` · ${rule.context.delivery_location}` : ""}</p>}
            <p>{text.feeEffect}</p>
          </div>}
          {rule.value.kind === "usage" && <div className="space-y-2 text-sm">
            <p className="text-xl font-semibold">{text.dailyRate}: {decimal(rule.value.daily_rate)} {"base_unit" in rule.context ? text.units[rule.context.base_unit] : ""}</p>
            <p>{text.slowest}: {decimal(rule.value.slowest_daily_rate)} {"base_unit" in rule.context ? text.units[rule.context.base_unit] : ""}</p>
            <p>{text.usageEffect}</p>
          </div>}
          {rule.value.kind === "quote_accuracy" && <div className="space-y-2 text-sm">
            <p className="text-xl font-semibold">{text.mismatches}: {rule.value.mismatch_count} / {rule.value.checked_count}</p>
            <p>{text.checked}</p><p>{text.accuracyEffect}</p>
          </div>}
        </>}
        <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
          {rule.uncertainty.map((code) => <li key={code}>{uncertainty[code] ?? text.unknownLimit}</li>)}
        </ul>
        {visible && rule.scope === "private" && <section className="space-y-3" aria-label={text.evidence}>
          <h4 className="font-medium">{text.evidence}</h4>
          <div className="flex flex-wrap gap-2">{rule.evidence.map((reference, index) => <Button key={`${reference.record_id}:${reference.revision}`} variant="outline" onClick={() => setSource(reference)}>{text.source} {index + 1}</Button>)}</div>
          <details className="text-xs text-muted-foreground"><summary className="cursor-pointer py-2">{text.references}</summary><ul className="space-y-2">{rule.evidence.map((reference) => <li key={`${reference.record_id}:${reference.revision}`} className="break-all">{reference.record_id} · {text.version} {reference.revision}<br />{reference.source_id}</li>)}</ul></details>
          {source && <SourceViewer key={source.source_id} business={source.business_id} sourceId={source.source_id} locale={locale} />}
        </section>}
        {rule.history.length > 0 && <details className="text-sm"><summary className="cursor-pointer py-2">{text.history}</summary><ol className="space-y-2 pt-2">{rule.history.map((item) => <li key={item.revision}>{text.states[item.state]} · {date(item.reviewed_at, locale)} · {text.version} {item.revision}</li>)}</ol></details>}
        {canReview && visible && ["proposed", "accepted"].includes(rule.state) && <div className="space-y-3 border-t pt-4">
          {rule.state === "proposed" && <>
            {!rule.eligible && <p className="text-sm">{text.insufficient}</p>}
            <label className="flex items-start gap-3 text-sm"><input type="checkbox" className="mt-1 size-4 shrink-0" checked={acknowledged} disabled={pending || !rule.eligible || !rule.context_name} onChange={(event) => setAcknowledged(event.target.checked)} />{text.acknowledge}</label>
            <div className="flex flex-wrap gap-3"><Button disabled={pending || !acknowledged || !rule.eligible} onClick={() => act("accept")}>{text.accept}</Button><Button variant="outline" disabled={pending} onClick={() => act("reject")}>{text.reject}</Button></div>
          </>}
          {rule.state === "accepted" && <Button variant="outline" disabled={pending} onClick={() => act("revoke")}>{text.revoke}</Button>}
        </div>}
      </CardContent>
    </Card>
  </article>;
}
