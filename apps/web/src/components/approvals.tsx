"use client";

import { useEffect, useState } from "react";

import { QuoteComparison } from "@/components/quote-comparison";
import { ActivityList } from "@/components/activity-list";
import { SourceUpload } from "@/components/source-upload";
import { Button } from "@/components/ui/button";
import { ApiError, apiRequest } from "@/lib/api/client";
import type { components } from "@/lib/api/generated";
import { messages, type Locale } from "@/lib/i18n";
import { createClient } from "@/lib/supabase/client";

type Identity = components["schemas"]["Identity"];
type NeedPage = components["schemas"]["NeedPage"];

export function Approvals({ locale }: { locale: Locale }) {
  const text = messages[locale];
  const [identity, setIdentity] = useState<Identity | null>(null);
  const [business, setBusiness] = useState("");
  const [page, setPage] = useState<NeedPage | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [activityRevision, setActivityRevision] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    apiRequest<Identity>("/v1/me", { signal: controller.signal })
      .then((value) => {
        if (controller.signal.aborted) return;
        setIdentity(value);
        setBusiness(value.memberships[0]?.business_id ?? "");
      })
      .catch((error) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 401)
          window.location.replace(`/${locale}/sign-in`);
        else setError(text.unavailable);
      });
    return () => controller.abort();
  }, [locale, text.unavailable]);

  useEffect(() => {
    const {
      data: { subscription },
    } = createClient().auth.onAuthStateChange((event, session) => {
      if (
        event === "SIGNED_OUT" ||
        (event === "SIGNED_IN" &&
          identity &&
          session?.user.id !== identity.subject)
      ) {
        setIdentity(null);
        setPage(null);
        window.location.replace(`/${locale}/sign-in`);
      }
    });
    return () => subscription.unsubscribe();
  }, [identity, locale]);

  useEffect(() => {
    if (!business) return;
    const controller = new AbortController();
    apiRequest<NeedPage>(
      `/v1/businesses/${business}/needs${cursor ? `?cursor=${cursor}` : ""}`,
      {
        signal: controller.signal,
      },
    )
      .then((value) => {
        if (!controller.signal.aborted) setPage(value);
      })
      .catch(() => {
        if (!controller.signal.aborted) setError(text.unavailable);
      });
    return () => controller.abort();
  }, [business, cursor, activityRevision, text.unavailable]);

  const membership = identity?.memberships.find(
    (item) => item.business_id === business,
  );
  return (
    <main
      lang={locale}
      className="mx-auto w-full max-w-5xl space-y-8 px-5 py-10 sm:px-8"
    >
      <header className="flex flex-wrap items-start justify-between gap-5">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.18em]">
            Orbyte Collective
          </p>
          <h1 className="mt-3 font-serif text-4xl">{text.approvals}</h1>
        </div>
        <a
          href={`/${locale}/sign-in`}
          className="text-sm underline underline-offset-4"
        >
          {text.account}
        </a>
      </header>
      {identity && identity.memberships.length > 0 && (
        <label className="block max-w-sm text-sm">
          {text.workspace}
          <select
            value={business}
            className="mt-2 min-h-11 w-full rounded-md border bg-card px-3"
            onChange={(event) => {
              setPage(null);
              setCursor(null);
              setError("");
              setBusiness(event.target.value);
            }}
          >
            {identity.memberships.map((item) => (
              <option key={item.business_id} value={item.business_id}>
                {item.name}
              </option>
            ))}
          </select>
        </label>
      )}
      {error && (
        <p role="alert" className="text-destructive">
          {error}
        </p>
      )}
      {!identity && !error && <p role="status">{text.loading}</p>}
      {identity && !identity.memberships.length && <p>{text.noMembership}</p>}
      {business && membership && <SourceUpload key={business} business={business} locale={locale} onChanged={() => setActivityRevision((value) => value + 1)} />}
      {business && !page && !error && <p role="status">{text.loading}</p>}
      {page?.items.length === 0 && <p>{text.noNeeds}</p>}
      {page?.items.map((need) => (
        <QuoteComparison
          key={`${business}:${need.id}:${need.revision}:${activityRevision}`}
          need={need}
          locale={locale}
          owner={membership?.role === "owner"}
          onSelected={(selected) => {
            setActivityRevision((value) => value + 1);
            setPage((current) =>
              current
                ? {
                    ...current,
                    items: current.items.map((item) =>
                      item.id === selected.id ? selected : item,
                    ),
                  }
                : null,
            );
          }}
        />
      ))}
      {page?.next_cursor && (
        <Button
          variant="outline"
          onClick={() => {
            setCursor(page.next_cursor ?? null);
            setPage(null);
          }}
        >
          {text.nextPage}
        </Button>
      )}
      {business && <ActivityList key={`${business}:${activityRevision}`} business={business} locale={locale} />}
    </main>
  );
}
