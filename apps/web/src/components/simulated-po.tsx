"use client";

import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { apiRequest } from "@/lib/api/client";
import type { components } from "@/lib/api/generated";
import { date as formatDate, money } from "@/lib/format";
import { messages, type Locale } from "@/lib/i18n";

type Need = components["schemas"]["NeedView"];
type Offer = components["schemas"]["PoolOffer"] | components["schemas"]["UnavailablePoolOffer"];
type Order = components["schemas"]["VisiblePurchaseOrder"] | components["schemas"]["HiddenPurchaseOrder"];
type Result = components["schemas"]["ReleaseResult"];

export function SimulatedPO({ need, offer, owner, locale, onChanged }: {
  need: Need;
  offer?: Offer;
  owner: boolean;
  locale: Locale;
  onChanged: (result: Result) => void;
}) {
  const text = messages[locale];
  const [order, setOrder] = useState<Order | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const retry = useRef<{ body: string; key: string } | null>(null);
  const root = `/v1/businesses/${need.business_id}` as const;

  useEffect(() => {
    if (need.state !== "released" || offer) return;
    const controller = new AbortController();
    apiRequest<Order>(`${root}/needs/${need.id}/order`, { signal: controller.signal })
      .then((value) => { if (!controller.signal.aborted) setOrder(value); })
      .catch(() => { if (!controller.signal.aborted) setError(text.unavailable); });
    return () => controller.abort();
  }, [root, need.id, need.state, offer, refresh, text.unavailable]);

  const canRelease = owner && (offer
    ? offer.release_state === "ready" && offer.approval_state === "valid" && offer.member_state === "approved"
    : need.state === "selected" && !need.reserved_pool_id && need.approval_state === "valid");

  async function release() {
    setPending(true);
    setError("");
    const body = JSON.stringify(offer
      ? { expected_revision: offer.evaluation_revision }
      : { expected_revision: need.revision, approval_id: need.approval_id });
    if (retry.current?.body !== body) retry.current = { body, key: crypto.randomUUID() };
    try {
      const result = await apiRequest<Result>(offer
        ? `${root}/pools/${offer.pool_id}/release` : `${root}/needs/${need.id}/release`, {
        method: "POST", headers: { "Idempotency-Key": retry.current.key }, body,
      });
      onChanged(result);
      if (result.state !== "released") setError(text.refreshRequired);
    } catch { setError(text.refreshRequired); }
    finally { setPending(false); }
  }

  if (!canRelease && need.state !== "released" && !error) return null;
  if (offer && need.state === "released") return null;
  const date = (value: string) => formatDate(value, locale, true);
  return (
    <section className="space-y-3 rounded-lg border p-4" aria-label={text.simulatedPO}>
      <h3 className="font-semibold">{text.simulatedPO}</h3>
      <p className="text-sm">{text.simulationExplanation}</p>
      {error && <p role="alert" className="text-destructive">{error}</p>}
      {canRelease && <Button disabled={pending} onClick={() => void release()}>{offer ? text.createPoolPOs : text.createSoloPO}</Button>}
      {need.state === "released" && !order && !error && <p role="status">{text.loading}</p>}
      {order && <>
        <p role="status">{text.pool_released}</p>
        <p className="break-all text-xs">{text.orderReference}: {order.id}</p>
        <p>{text.quantity}: {order.quantity} {order.base_unit}</p>
        <p className="text-sm">{text.issuedAt}: {date(order.issued_at)}</p>
        {order.price_visibility === "hidden" ? <p>{text.pricesHidden}</p> : <>
          <p className="font-medium">{order.supplier_name}</p>
          <dl className="grid grid-cols-2 gap-3 text-sm">
            <div><dt>{text.agreed}</dt><dd>{money(order.amount)}</dd></div>
            <div><dt>{text.decision}</dt><dd>{money(order.decision_amount)}</dd></div>
            <div><dt>{text.issuedUnitPrice}</dt><dd>RM {order.cost.unit_price}</dd></div>
            <div><dt>{text.issuedTier}</dt><dd>{order.cost.tier_min_quantity}</dd></div>
            <div><dt>{text.delivery}</dt><dd>{money(order.cost.delivery_fee)}</dd></div>
            <div><dt>{text.credit}</dt><dd>{order.terms.credit_days} {text.days}</dd></div>
          </dl>
          <p className="text-sm">{text.poolDelivery}: {date(order.terms.delivery_window.start)} – {date(order.terms.delivery_window.end)}</p>
        </>}
      </>}
      {error && need.state === "released" && <Button variant="outline" onClick={() => { setError(""); setRefresh((value) => value + 1); }}>{text.retry}</Button>}
    </section>
  );
}
