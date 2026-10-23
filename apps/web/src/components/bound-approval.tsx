"use client";

import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError, apiRequest } from "@/lib/api/client";
import type { components } from "@/lib/api/generated";
import { money } from "@/lib/format";
import { messages, type Locale } from "@/lib/i18n";

type Offer = components["schemas"]["PoolOffer"] | components["schemas"]["UnavailablePoolOffer"];
type Approval = components["schemas"]["VisibleApproval"] | components["schemas"]["HiddenApproval"];
type Result = components["schemas"]["ApprovalCommandResult"];

export function BoundApproval({ business, offer, locale, onChanged }: {
  business: string;
  offer: Offer;
  locale: Locale;
  onChanged: (result: Result) => void;
}) {
  const text = messages[locale];
  const [approval, setApproval] = useState<Approval | null>(null);
  const [cap, setCap] = useState("");
  const [expiry, setExpiry] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const retry = useRef<{ body: string; key: string } | null>(null);

  useEffect(() => {
    if (!offer.approval_id) return;
    const controller = new AbortController();
    apiRequest<Approval>(`/v1/hub/approvals/${offer.approval_id}`, { signal: controller.signal })
      .then((value) => { if (!controller.signal.aborted) setApproval(value); })
      .catch(() => { if (!controller.signal.aborted) setError(text.unavailable); });
    return () => controller.abort();
  }, [offer.approval_id, offer.evaluation_revision, text.unavailable]);

  const date = (value: string) => new Intl.DateTimeFormat(locale, {
    dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Kuala_Lumpur",
  }).format(new Date(value));
  const active = ["offered", "approved", "awaiting_reapproval"].includes(offer.member_state ?? "offered");

  async function approve(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setError("");
    try {
      const body = JSON.stringify({
        need_id: offer.need_id, pool_id: offer.pool_id, cap,
        expires_at: new Date(`${expiry}:00+08:00`).toISOString(),
        expected_evaluation_revision: offer.evaluation_revision,
      });
      if (retry.current?.body !== body) retry.current = { body, key: crypto.randomUUID() };
      const result = await apiRequest<Result>(`/v1/businesses/${business}/approvals`, {
        method: "POST", headers: { "Idempotency-Key": retry.current.key }, body,
      });
      onChanged(result);
      setCap("");
      setExpiry("");
      if (result.status === "refresh_required") setError(text.refreshRequired);
    } catch (error) {
      setError(error instanceof ApiError && error.status === 422 ? text.approvalInvalid : text.refreshRequired);
    } finally { setPending(false); }
  }

  return (
    <section className="space-y-3 border-t pt-4" aria-label={text.boundApproval}>
      <h4 className="font-semibold">{text.boundApproval}</h4>
      <p role="status">{text[`approval_${offer.approval_state}`]}</p>
      {offer.approval_reason === "CAP_EXCEEDED" && <p>{text.capExceeded}</p>}
      {offer.approval_reason === "BOUND_TERMS_CHANGED" && <p>{text.boundTermsChanged}</p>}
      {approval?.price_visibility === "visible" && approval.id === offer.approval_id && (
        <div className="space-y-1 text-sm">
          <p>{text.savedCap}: {money(approval.cap)}</p>
          <p>{text.approvalExpiry}: {date(approval.expires_at)}</p>
          <p className="break-all text-xs text-muted-foreground">{text.approvalReference}: {approval.id}</p>
        </div>
      )}
      {error && <p role="alert" className="text-destructive">{error}</p>}
      {active && offer.eligibility === "eligible" && offer.price_visibility === "visible" && offer.approval_state !== "valid" && (
        <form className="space-y-3" onSubmit={approve}>
          <p className="text-sm">{text.approvalScope}</p>
          <label className="block text-sm">
            {text.maximumCap}
            <Input className="mt-2" inputMode="decimal" pattern="[0-9]+(\.[0-9]{1,2})?" required value={cap} onChange={(event) => setCap(event.target.value)} />
          </label>
          <label className="block text-sm">
            {text.expiryMalaysia}
            <Input className="mt-2" type="datetime-local" required value={expiry} onChange={(event) => setExpiry(event.target.value)} />
          </label>
          <Button type="submit" disabled={pending}>
            {offer.approval_state === "void" || offer.approval_state === "expired" ? text.approveAgain : text.approveOrder}
          </Button>
        </form>
      )}
    </section>
  );
}
