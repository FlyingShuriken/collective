"use client";

import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { ApiError, apiRequest } from "@/lib/api/client";
import type { components } from "@/lib/api/generated";
import { money } from "@/lib/format";
import { messages, type Locale } from "@/lib/i18n";

type Need = components["schemas"]["NeedView"];
type Offer =
  components["schemas"]["PoolOffer"] | components["schemas"]["HiddenPoolOffer"];
type Discovery = components["schemas"]["DiscoveryResult"];

export function PoolOffer({
  need,
  locale,
  owner,
  onReserved,
}: {
  need: Need;
  locale: Locale;
  owner: boolean;
  onReserved: (need: Need) => void;
}) {
  const text = messages[locale];
  const [offer, setOffer] = useState<Offer | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [failure, setFailure] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const root = `/v1/businesses/${need.business_id}` as const;

  useEffect(() => {
    if (!need.reserved_pool_id) return;
    const controller = new AbortController();
    apiRequest<Offer>(`${root}/pools/${need.reserved_pool_id}/offer`, {
      signal: controller.signal,
    })
      .then((result) => {
        if (!controller.signal.aborted) setOffer(result);
      })
      .catch(() => {
        if (!controller.signal.aborted) setError(text.unavailable);
      });
    return () => controller.abort();
  }, [need.reserved_pool_id, root, refresh, text.unavailable]);

  async function discover() {
    setPending(true);
    setError("");
    setFailure(null);
    try {
      const result = await apiRequest<Discovery>(`${root}/pools/discover`, {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({
          need_id: need.id,
          expected_revision: need.revision,
        }),
      });
      if (result.offer) {
        setOffer(result.offer);
        onReserved(result.need);
      } else setFailure(result.failure_codes[0] ?? "NO_ELIGIBLE_POOL");
    } catch (error) {
      setError(
        error instanceof ApiError && error.status === 409
          ? text.refreshRequired
          : text.unavailable,
      );
    } finally {
      setPending(false);
    }
  }

  if (
    !need.reserved_pool_id &&
    (!owner || need.state !== "selected" || !need.pool_opt_in)
  )
    return null;
  const date = (value: string) =>
    new Intl.DateTimeFormat(locale, {
      dateStyle: "medium",
      timeStyle: "short",
      timeZone: "Asia/Kuala_Lumpur",
    }).format(new Date(value));
  return (
    <section
      className="space-y-4 rounded-lg border border-primary/20 bg-primary/5 p-4"
      aria-label={text.poolHeading}
    >
      <h3 className="font-semibold">{text.poolHeading}</h3>
      {error && (
        <p role="alert" className="text-destructive">
          {error}
        </p>
      )}
      {failure && (
        <p role="status">
          {failure === "DISCOVERY_REVIEW_REQUIRED"
            ? text.poolSearchReview
            : text.noPool}
        </p>
      )}
      {!need.reserved_pool_id && (
        <>
          <p className="text-sm text-muted-foreground">
            {text.poolExplanation}
          </p>
          <Button onClick={discover} disabled={pending}>
            {pending ? text.poolSearching : text.findPool}
          </Button>
        </>
      )}
      {need.reserved_pool_id && !offer && !error && (
        <p role="status">{text.loading}</p>
      )}
      {offer && (
        <>
          <p className="text-sm">
            {text.poolMembers}: {offer.member_count}
          </p>
          <p className="text-sm">
            {text.poolDeadline}: {date(offer.deadline)}
          </p>
          <p role="status">{text[`pool_${offer.release_state}`]}</p>
          <p className="text-xs text-muted-foreground">
            {text.evaluatedAt}: {date(offer.evaluated_at)}
          </p>
          {offer.eligibility !== "eligible" && <p>{text.poolNeedsReview}</p>}
          {offer.price_visibility === "hidden" ? (
            <p>{text.pricesHidden}</p>
          ) : (
            <>
              <p className="font-medium">{offer.supplier_name}</p>
              <dl className="grid gap-3 sm:grid-cols-2">
                <div>
                  <dt className="text-sm">{text.poolOwnCost}</dt>
                  <dd className="mt-1 text-2xl font-semibold">
                    {money(offer.decision_delivered_cost)}
                  </dd>
                </div>
                <div>
                  <dt className="text-sm">{text.poolSaving}</dt>
                  <dd className="mt-1 text-2xl font-semibold">
                    {money(offer.pooling_saving)}
                  </dd>
                </div>
                <div>
                  <dt className="text-sm">{text.history}</dt>
                  <dd>{money(offer.historical_comparable_cost)}</dd>
                </div>
                <div>
                  <dt className="text-sm">{text.solo}</dt>
                  <dd>{money(offer.current_solo_comparable_cost)}</dd>
                </div>
              </dl>
              <p className="text-sm">
                {text.credit}: {offer.credit_days} {text.days}
              </p>
              <p className="text-sm">
                {text.poolDelivery}: {date(offer.delivery_window.start)} –{" "}
                {date(offer.delivery_window.end)}
              </p>
            </>
          )}
          {offer.release_state !== "released" && (
            <p className="text-sm text-muted-foreground">{text.poolNoOrder}</p>
          )}
        </>
      )}
      {need.reserved_pool_id && (
        <Button
          variant="outline"
          onClick={() => {
            setError("");
            setRefresh((value) => value + 1);
          }}
        >
          {text.refreshOffer}
        </Button>
      )}
    </section>
  );
}
