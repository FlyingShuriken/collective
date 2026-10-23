"use client";

import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { apiRequest } from "@/lib/api/client";
import type { components } from "@/lib/api/generated";
import { messages, type Locale } from "@/lib/i18n";

export function ActivityList({ business, locale }: { business: string; locale: Locale }) {
  const text = messages[locale];
  const [page, setPage] = useState<components["schemas"]["ActivityPage"] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    apiRequest<components["schemas"]["ActivityPage"]>(`/v1/businesses/${business}/events${cursor ? `?cursor=${cursor}` : ""}`, { signal: controller.signal })
      .then((value) => { if (!controller.signal.aborted) setPage(value); })
      .catch(() => { if (!controller.signal.aborted) setError(text.unavailable); });
    return () => controller.abort();
  }, [business, cursor, refresh, text.unavailable]);

  return (
    <section className="space-y-4 border-t pt-6" aria-label={text.activity}>
      <h2 className="font-serif text-2xl">{text.activity}</h2>
      {error && <p role="alert" className="text-destructive">{error}</p>}
      {!page && !error && <p role="status">{text.loading}</p>}
      {page?.items.length === 0 && <p>{text.noActivity}</p>}
      <ol className="space-y-3">
        {page?.items.map((event) => <li key={event.id} className="space-y-1 rounded-lg border p-4">
          <p className="text-sm">{event.summary}</p>
          <time className="text-xs text-muted-foreground" dateTime={event.occurred_at}>
            {new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Kuala_Lumpur" }).format(new Date(event.occurred_at))}
          </time>
        </li>)}
      </ol>
      {page?.next_cursor && <Button variant="outline" onClick={() => { setCursor(page.next_cursor ?? null); setPage(null); }}>{text.nextPage}</Button>}
      <Button variant="outline" onClick={() => { setCursor(null); setError(""); setRefresh((value) => value + 1); }}>{text.refreshActivity}</Button>
    </section>
  );
}
