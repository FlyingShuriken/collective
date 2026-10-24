"use client";

import { useEffect, useState } from "react";

import { apiRequest } from "@/lib/api/client";
import type { components } from "@/lib/api/generated";
import { money } from "@/lib/format";
import { messages, type Locale } from "@/lib/i18n";

type Audit = components["schemas"]["VisibleSelfAudit"] | components["schemas"]["HiddenSelfAudit"];

export function SelfAudit({ business, locale }: { business: string; locale: Locale }) {
  const text = messages[locale].audit;
  const [value, setValue] = useState<Audit | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    apiRequest<Audit>(`/v1/businesses/${business}/self-audit`, { signal: controller.signal })
      .then((result) => { if (!controller.signal.aborted) setValue(result); })
      .catch(() => { if (!controller.signal.aborted) setError(true); });
    return () => controller.abort();
  }, [business]);
  return <section className="space-y-4 rounded-xl border bg-card p-5" aria-label={text.title}>
    <h2 className="font-serif text-2xl">{text.title}</h2>
    <p className="text-sm text-muted-foreground">{text.explanation}</p>
    {error && <p role="alert">{messages[locale].unavailable}</p>}
    {!value && !error && <p role="status">{messages[locale].loading}</p>}
    {value && <>
      <p className="text-sm">{value.period_start && value.period_end
        ? `${text.period}: ${new Date(value.period_start).toLocaleDateString(locale)} – ${new Date(value.period_end).toLocaleDateString(locale)}` : text.empty}</p>
      <dl className="grid gap-4 text-sm sm:grid-cols-2">
        <div><dt>{text.purchases}</dt><dd className="font-semibold">{value.purchase_records}</dd></div>
        <div><dt>{text.delivered}</dt><dd className="font-semibold">{value.delivered_records}</dd></div>
        <div><dt>{text.checked}</dt><dd className="font-semibold">{value.checked_records} / {value.delivered_records}</dd></div>
        <div><dt>{text.mismatched}</dt><dd className="font-semibold">{value.mismatched_records} / {value.checked_records}</dd></div>
        <div><dt>{text.unmatched}</dt><dd className="font-semibold">{value.unmatched_records}</dd></div>
        <div><dt>{text.incomplete}</dt><dd className="font-semibold">{value.incomplete_checks}</dd></div>
        <div><dt>{text.variances}</dt><dd className="font-semibold">{value.confirmed_variances}{value.price_visibility === "visible" && ` · ${money(value.confirmed_variance_amount)}`}</dd></div>
        <div><dt>{text.recoveries}</dt><dd className="font-semibold">{value.documented_recoveries}{value.price_visibility === "visible" && ` · ${money(value.documented_recovery_amount)}`}</dd></div>
      </dl>
      {value.price_visibility === "hidden" && <p className="text-sm">{text.hidden}</p>}
    </>}
  </section>;
}
