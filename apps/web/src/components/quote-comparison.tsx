"use client";

import { useEffect, useState } from "react";

import { PoolOffer } from "@/components/pool-offer";
import { BoundApproval } from "@/components/bound-approval";
import { SimulatedPO } from "@/components/simulated-po";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { apiRequest } from "@/lib/api/client";
import type { components } from "@/lib/api/generated";
import { messages, type Locale } from "@/lib/i18n";
import { money } from "@/lib/format";

type Need = components["schemas"]["NeedView"];
type Comparison =
  | components["schemas"]["NeedComparisons"]
  | components["schemas"]["HiddenNeedData"];
type Options =
  | components["schemas"]["NeedOptions"]
  | components["schemas"]["HiddenNeedData"];

export function QuoteComparison({
  need,
  locale,
  owner,
  onSelected,
}: {
  need: Need;
  locale: Locale;
  owner: boolean;
  onSelected: (need: Need) => void;
}) {
  const text = messages[locale];
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [options, setOptions] = useState<Options | null>(null);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const [solo, setSolo] = useState<components["schemas"]["OfferComparison"] | null>(null);
  const path = `/v1/businesses/${need.business_id}/needs/${need.id}` as const;

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      apiRequest<Comparison>(`${path}/comparisons`, {
        signal: controller.signal,
      }),
      apiRequest<Options>(`${path}/options`, { signal: controller.signal }),
    ])
      .then(([comparison, options]) => {
        if (controller.signal.aborted) return;
        setComparison(comparison);
        setOptions(options);
      })
      .catch(() => {
        if (!controller.signal.aborted) setError(text.unavailable);
      });
    return () => controller.abort();
  }, [path, text.unavailable]);

  async function select(quantity: string) {
    setPending(true);
    setError("");
    try {
      const result = await apiRequest<Need>(`${path}/select`, {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ expected_revision: need.revision, quantity }),
      });
      onSelected(result);
    } catch {
      setError(text.refreshRequired);
    } finally {
      setPending(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="font-serif text-2xl">
          <h2>{need.name}</h2>
        </CardTitle>
        <p>
          {text.quantity}: {need.quantity} {need.base_unit}
        </p>
        <p className="text-sm text-muted-foreground">
          {text[`state_${need.state}`]}
        </p>
      </CardHeader>
      <CardContent className="space-y-6">
        {error && (
          <p role="alert" className="text-destructive">
            {error}
          </p>
        )}
        {!comparison && !error && <p role="status">{text.loading}</p>}
        {comparison?.price_visibility === "hidden" && (
          <p>{text.pricesHidden}</p>
        )}
        {comparison?.price_visibility === "visible" && (
          <>
            <p className="text-sm text-muted-foreground">{text.likeForLike}</p>
            <dl className="grid grid-cols-2 gap-4 rounded-lg bg-muted p-4">
              <div>
                <dt className="text-sm">{text.history}</dt>
                <dd className="mt-1 text-xl font-semibold">
                  {money(comparison.historical_comparable_cost)}
                </dd>
              </div>
              <div>
                <dt className="text-sm">{text.solo}</dt>
                <dd className="mt-1 text-xl font-semibold">
                  {money(comparison.best_solo_cost)}
                </dd>
              </div>
            </dl>
            {!comparison.historical_comparable_cost && <p>{text.noBaseline}</p>}
            <div className="grid gap-4 sm:grid-cols-2">
              {comparison.offers.map((offer) => (
                <section key={offer.quote_id} className="rounded-lg border p-4">
                  <h3 className="font-semibold">{offer.supplier_name}</h3>
                  <p className="mt-1 text-sm text-muted-foreground">
                    {text.credit}: {offer.credit_days ?? "—"} {text.days}
                  </p>
                  <dl className="mt-4 space-y-2 text-sm">
                    <div className="flex justify-between gap-3">
                      <dt>{text.goods}</dt>
                      <dd>{money(offer.cost.goods)}</dd>
                    </div>
                    <div className="flex justify-between gap-3">
                      <dt>{text.delivery}</dt>
                      <dd>{money(offer.cost.delivery_fee)}</dd>
                    </div>
                    <div className="flex justify-between gap-3">
                      <dt>{text.tax}</dt>
                      <dd>{money(offer.cost.tax)}</dd>
                    </div>
                    <div className="flex justify-between gap-3">
                      <dt>{text.discount}</dt>
                      <dd>{money(offer.cost.discount)}</dd>
                    </div>
                    <div className="flex justify-between gap-3">
                      <dt>{text.surcharge}</dt>
                      <dd>{money(offer.cost.surcharge)}</dd>
                    </div>
                    <div className="flex justify-between gap-3">
                      <dt>{text.agreed}</dt>
                      <dd>{money(offer.cost.agreed_cost)}</dd>
                    </div>
                    <div className="flex justify-between gap-3">
                      <dt>{text.expected}</dt>
                      <dd>{money(offer.cost.expected_cost)}</dd>
                    </div>
                    <div className="flex justify-between gap-3 border-t pt-2 font-semibold">
                      <dt>{text.decision}</dt>
                      <dd>{money(offer.cost.decision_cost)}</dd>
                    </div>
                  </dl>
                  {offer.cost.fee_unverified &&
                    offer.cost.agreed_cost !== null && (
                      <p className="mt-3 text-sm text-muted-foreground">
                        {text.feeUnverified}
                      </p>
                    )}
                  {offer.failure_codes.length > 0 && (
                    <p className="mt-3 text-sm">{text.unavailableQuote}</p>
                  )}
                  {offer.conditions && (
                    <p className="mt-3 text-sm text-muted-foreground">
                      {offer.conditions}
                    </p>
                  )}
                  {owner && need.state === "selected" && !need.reserved_pool_id && need.approval_state !== "valid" && !offer.failure_codes.length && (
                    <Button className="mt-4" variant="outline" onClick={() => setSolo(offer)}>{text.reviewSolo}</Button>
                  )}
                </section>
              ))}
            </div>
            <p className="text-xs text-muted-foreground">
              {text.evaluatedAt}:{" "}
              {new Intl.DateTimeFormat(locale, {
                dateStyle: "medium",
                timeStyle: "short",
                timeZone: "Asia/Kuala_Lumpur",
              }).format(new Date(comparison.evaluated_at))}
            </p>
          </>
        )}
        {options?.price_visibility === "visible" &&
          !["reserved", "released"].includes(need.state) && (
            <section className="space-y-3 border-t pt-5">
              <h3 className="font-semibold">{text.quantityOptions}</h3>
              {options.replenishment.run_out_at && (
                <p className="text-sm">
                  {text.runout}:{" "}
                  {new Intl.DateTimeFormat(locale, {
                    dateStyle: "medium",
                    timeZone: "Asia/Kuala_Lumpur",
                  }).format(new Date(options.replenishment.run_out_at))}
                </p>
              )}
              {options.failure_codes.length > 0 && (
                <p>
                  {options.stated_quantity_valid
                    ? text.noConfirmedQuote
                    : text.needMoreData}
                </p>
              )}
              {owner &&
                need.state === "suggested" &&
                options.stated_quantity_valid &&
                !options.options.length && (
                  <Button
                    onClick={() => select(need.quantity)}
                    disabled={pending}
                  >
                    {text.keepStatedQuantity}
                  </Button>
                )}
              {options.options.map((option) => (
                <div
                  key={option.quantity}
                  className="flex flex-wrap items-center justify-between gap-3 rounded-lg border p-4"
                >
                  <div>
                    <p className="font-medium">
                      {option.quantity} {need.base_unit} · {money(option.cost)}
                    </p>
                    <p className="mt-1 text-sm text-muted-foreground">
                      {text.extraCash}: {money(option.extra_cash_now)} ·{" "}
                      {text.repeatSaving}:{" "}
                      {money(option.saving_vs_repeat_orders)}
                    </p>
                    {option.assumptions.length > 0 && (
                      <p className="mt-1 max-w-lg text-sm text-muted-foreground">
                        {text.repeatAssumption}
                      </p>
                    )}
                  </div>
                  {owner && need.state === "suggested" && (
                    <Button
                      onClick={() => select(option.quantity)}
                      disabled={pending}
                      aria-label={`${text.selectQuantity} ${option.quantity} ${need.base_unit}`}
                    >
                      {text.selectQuantity}
                    </Button>
                  )}
                </div>
              ))}
            </section>
          )}
        {owner && need.state === "selected" && !need.reserved_pool_id && (solo || need.approval_id) && (
          <BoundApproval need={need} quote={solo} locale={locale} onChanged={(result) => onSelected(result.need)} />
        )}
        <SimulatedPO need={need} owner={owner} locale={locale} onChanged={(result) => onSelected(result.need)} />
        <PoolOffer
          need={need}
          locale={locale}
          owner={owner}
          onReserved={onSelected}
        />
      </CardContent>
    </Card>
  );
}
