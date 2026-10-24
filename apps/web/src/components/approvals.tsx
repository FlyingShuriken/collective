"use client";

import { useEffect, useState } from "react";

import { QuoteComparison } from "@/components/quote-comparison";
import { ActivityList } from "@/components/activity-list";
import { RuleList } from "@/components/rule-card";
import { SourceUpload } from "@/components/source-upload";
import { BusinessSwitcher } from "@/components/business-switcher";
import { Ledger } from "@/components/ledger";
import { SupplierMessages } from "@/components/supplier-messages";
import { Button } from "@/components/ui/button";
import { ApiError, apiRequest } from "@/lib/api/client";
import type { components } from "@/lib/api/generated";
import { messages, type Locale } from "@/lib/i18n";
import { createClient } from "@/lib/supabase/client";

type Identity = components["schemas"]["Identity"];
type NeedPage = components["schemas"]["NeedPage"];

export type ProductScreen = "inbox" | "approvals" | "ledger" | "rules" | "activity";

export function Approvals({ locale, screen = "approvals" }: { locale: Locale; screen?: ProductScreen }) {
  const text = messages[locale];
  const [identity, setIdentity] = useState<Identity | null>(null);
  const [business, setBusiness] = useState("");
  const [network, setNetwork] = useState("");
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
        const selected = new URLSearchParams(window.location.search).get("business");
        setBusiness(value.memberships.find((item) => item.business_id === selected)?.business_id ?? value.memberships[0]?.business_id ?? "");
        setNetwork(value.networks?.[0]?.network_id ?? "");
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
        setBusiness("");
        setNetwork("");
        window.location.replace(`/${locale}/sign-in`);
      }
    });
    return () => subscription.unsubscribe();
  }, [identity, locale]);

  useEffect(() => {
    if (!business || screen !== "approvals") return;
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
  }, [business, screen, cursor, activityRevision, text.unavailable]);

  const membership = identity?.memberships.find(
    (item) => item.business_id === business,
  );
  const admin = !!identity && !identity.memberships.length && !!identity.networks?.length;
  const active = admin ? "rules" : screen;
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
          <h1 className="mt-3 font-serif text-4xl">{admin ? text.rules.heading : text.screens[active]}</h1>
        </div>
        <a
          href={`/${locale}/sign-in`}
          className="text-sm underline underline-offset-4"
        >
          {text.account}
        </a>
      </header>
      {identity && <nav aria-label={text.screens.navigation} className="flex flex-wrap gap-2 border-b pb-3">
        {(["inbox", "approvals", "ledger", "rules", "activity"] as const).filter((item) => !admin || item === "rules").map((item) => <a key={item}
          href={`/${locale}/${item}${business ? `?business=${business}` : ""}`}
          aria-current={item === active ? "page" : undefined}
          className={`inline-flex min-h-11 items-center rounded-md px-3 text-sm ${item === active ? "bg-foreground text-background" : "border"}`}>{text.screens[item]}</a>)}
      </nav>}
      {identity && identity.memberships.length > 0 && (
        <BusinessSwitcher memberships={identity.memberships} value={business} locale={locale}
            onChange={(value) => {
              setPage(null);
              setCursor(null);
              setError("");
              setBusiness(value);
            }}
        />
      )}
      {error && (
        <p role="alert" className="text-destructive">
          {error}
        </p>
      )}
      {!identity && !error && <p role="status">{text.loading}</p>}
      {identity && !identity.memberships.length && !identity.networks?.length && <p>{text.noMembership}</p>}
      {business && membership && active === "inbox" && <SourceUpload key={`${identity?.subject}:${business}`} business={business} locale={locale} owner={membership.role === "owner"} onChanged={() => setActivityRevision((value) => value + 1)} />}
      {business && membership && active === "inbox" && <SupplierMessages key={`${identity?.subject}:${business}:messages`} business={business} locale={locale} owner={membership.role === "owner"} />}
      {active === "approvals" && business && !page && !error && <p role="status">{text.loading}</p>}
      {active === "approvals" && page?.items.length === 0 && <p>{text.noNeeds} <a className="underline" href={`/${locale}/inbox?business=${business}`}>{text.ledger.next}</a></p>}
      {active === "approvals" && page?.items.map((need) => (
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
      {active === "approvals" && page?.next_cursor && (
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
      {active === "ledger" && business && membership && <Ledger key={`${identity?.subject}:${business}`} business={business} locale={locale} />}
      {active === "rules" && business && membership && <RuleList key={`${identity?.subject}:${business}:${membership.role}:${membership.can_view_prices}`} scope="private" scopeId={business} locale={locale} canReview={membership.role === "owner"} refresh={activityRevision} onChanged={() => setActivityRevision((value) => value + 1)} />}
      {active === "rules" && !!identity?.networks?.length && <>
        <label className="block max-w-sm text-sm">{text.rules.networkWorkspace}<select className="mt-2 min-h-11 w-full rounded-md border bg-card px-3" value={network} onChange={(event) => setNetwork(event.target.value)}>{identity.networks.map((item) => <option key={item.network_id} value={item.network_id}>{item.name}</option>)}</select></label>
        {network && <RuleList key={`${identity.subject}:${network}`} scope="network" scopeId={network} locale={locale} canReview refresh={activityRevision} onChanged={() => setActivityRevision((value) => value + 1)} />}
      </>}
      {active === "activity" && business && membership && <ActivityList key={`${identity?.subject}:${business}:${activityRevision}`} business={business} locale={locale} />}
    </main>
  );
}
