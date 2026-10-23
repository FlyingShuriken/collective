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
  const [jobs, setJobs] = useState<components["schemas"]["JobPage"] | null>(null);
  const [jobCursor, setJobCursor] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      apiRequest<components["schemas"]["ActivityPage"]>(`/v1/businesses/${business}/events${cursor ? `?cursor=${cursor}` : ""}`, { signal: controller.signal }),
      apiRequest<components["schemas"]["JobPage"]>(`/v1/businesses/${business}/jobs?limit=10${jobCursor ? `&cursor=${jobCursor}` : ""}`, { signal: controller.signal }),
    ])
      .then(([events, tasks]) => { if (!controller.signal.aborted) { setPage(events); setJobs(tasks); } })
      .catch(() => { if (!controller.signal.aborted) setError(text.unavailable); });
    return () => controller.abort();
  }, [business, cursor, jobCursor, refresh, text.unavailable]);

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
      <section className="space-y-3" aria-label={text.backgroundTasks}>
        <h3 className="font-semibold">{text.backgroundTasks}</h3>
        <p className="text-sm text-muted-foreground">{text.taskNotApproval}</p>
        {jobs?.items.length === 0 && <p>{text.noJobs}</p>}
        {!jobs && !error && <p role="status">{text.loading}</p>}
        {jobs?.items.map((job) => <article key={job.id} className="space-y-1 rounded-lg border p-3 text-sm">
          <h4 className="font-medium">{job.kind === "reorder" ? text.reorderTask : job.kind === "expiry" ? text.expiryTask : text.backgroundTask}</h4>
          <p role="status">{job.lease_expired ? text.leaseExpired : job.status === "queued" && job.error_code ? text.retryScheduled : text[`job_${job.status}`]}</p>
          <p>{text.attempts}: {job.attempts}</p>
          {job.status === "queued" && job.error_code && <p>{text.nextRetry}: {new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Kuala_Lumpur" }).format(new Date(job.next_attempt_at))}</p>}
          {job.status === "failed" && <p>{text.taskNeedsReview}</p>}
        </article>)}
        {jobs?.next_cursor && <Button variant="outline" onClick={() => { setJobCursor(jobs.next_cursor ?? null); setJobs(null); }}>{text.moreTasks}</Button>}
      </section>
      <Button variant="outline" onClick={() => { setCursor(null); setJobCursor(null); setError(""); setRefresh((value) => value + 1); }}>{text.refreshActivity}</Button>
    </section>
  );
}
