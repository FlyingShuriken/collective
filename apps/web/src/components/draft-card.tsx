"use client";

import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError, apiRequest } from "@/lib/api/client";
import type { components } from "@/lib/api/generated";
import { messages, type Locale } from "@/lib/i18n";

type Draft = components["schemas"]["DraftView"];
type Hidden = components["schemas"]["HiddenDraft"];

export function DraftCard({ business, draftId, locale, owner, onChanged }: {
  business: string;
  draftId: string;
  locale: Locale;
  owner: boolean;
  onChanged: () => void;
}) {
  const [draft, setDraft] = useState<Draft | Hidden | null>(null);
  const copy = messages[locale].communication;
  const text = draft?.kind === "rfq" ? { ...messages[locale].invoices,
    query: copy.rfq, sent: copy.sent, queryText: copy.text, editQuery: copy.edit,
    saveQuery: copy.save, sendQuery: copy.send, reviewedQuery: copy.acknowledge,
    sendNotRecovery: copy.notOrder,
  } : messages[locale].invoices;
  const [content, setContent] = useState("");
  const [recipient, setRecipient] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const retry = useRef<{ target: string; body: string; key: string } | null>(null);
  const root = `/v1/businesses/${business}/drafts/${draftId}` as const;

  function install(value: Draft | Hidden) {
    setDraft(value); setAcknowledged(false);
    if (value.price_visibility === "visible") { setContent(value.text_content); setRecipient(value.recipient); }
  }

  useEffect(() => {
    const controller = new AbortController();
    apiRequest<Draft | Hidden>(root, { signal: controller.signal })
      .then((value) => { if (!controller.signal.aborted) install(value); })
      .catch(() => { if (!controller.signal.aborted) setError(text.unavailable); });
    return () => controller.abort();
  }, [root, refresh, text.unavailable]);

  async function mutate(action: "edit" | "send") {
    if (draft?.price_visibility !== "visible") return;
    setPending(true); setError("");
    const target = action === "edit" ? root : `${root}/send` as const;
    const body = JSON.stringify(action === "edit" ? {
      expected_revision: draft.revision, text_content: content, recipient,
    } : { expected_revision: draft.revision, recipient: draft.recipient, approved_hash: draft.approved_hash });
    if (retry.current?.target !== target || retry.current?.body !== body) retry.current = { target, body, key: crypto.randomUUID() };
    try {
      if (action === "edit") install(await apiRequest<Draft>(target, { method: "PATCH", body, headers: { "Idempotency-Key": retry.current.key } }));
      else {
        await apiRequest<components["schemas"]["MessageReceipt"]>(target, { method: "POST", body, headers: { "Idempotency-Key": retry.current.key } });
        install(await apiRequest<Draft>(root));
      }
      onChanged();
    } catch (error) { setError(error instanceof ApiError && error.status === 409 ? text.stale : text.unavailable); }
    finally { setPending(false); }
  }

  const visible = draft?.price_visibility === "visible";
  const dirty = visible && (content !== draft.text_content || recipient !== draft.recipient);
  return <section className="min-w-0 space-y-4 rounded-lg border p-4" aria-label={text.query}>
    <h4 className="font-semibold">{text.query}</h4>
    <p className="text-sm text-muted-foreground">{text.simulatedOnly}</p>
    {error && <p role="alert" className="text-destructive">{error}</p>}
    {!draft && !error && <p role="status">{messages[locale].loading}</p>}
    {draft?.price_visibility === "hidden" && <p>{messages[locale].pricesHidden}</p>}
    {visible && <>
      <p role="status" className="text-sm font-semibold">{draft.status === "sent_simulated" ? text.sent : text.unsent}</p>
      <p className="text-xs text-muted-foreground">{draft.interpretation_mode === "replay" ? text.replayQuery : text.aiQuery}</p>
      {draft.status === "draft" && owner ? <>
        <form className="space-y-3" aria-label={text.editQuery} onSubmit={(event) => { event.preventDefault(); void mutate("edit"); }}>
          <fieldset disabled={pending} className="space-y-3">
            <label className="block text-sm">{text.recipient}<Input className="mt-2" value={recipient} required maxLength={300} onChange={(event) => { setRecipient(event.target.value); setAcknowledged(false); }} /></label>
            <label className="block text-sm">{text.queryText}<textarea className="mt-2 min-h-48 w-full rounded border bg-card p-3" required maxLength={16000} value={content} onChange={(event) => { setContent(event.target.value); setAcknowledged(false); }} /></label>
            <Button type="submit" variant="outline" disabled={!dirty}>{text.saveQuery}</Button>
          </fieldset>
        </form>
        {dirty && <p className="text-sm">{text.saveBeforeSend}</p>}
        <label className="flex items-start gap-3 text-sm"><input type="checkbox" className="mt-1 size-5 shrink-0" disabled={pending || dirty} checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)} />{text.reviewedQuery}</label>
        <Button type="button" disabled={pending || dirty || !acknowledged} onClick={() => void mutate("send")}>{text.sendQuery}</Button>
      </> : <>
        <p className="break-words text-sm">{text.recipient}: {draft.recipient}</p>
        <p className="whitespace-pre-wrap break-words text-sm">{draft.text_content}</p>
      </>}
      <p className="text-sm">{text.sendNotRecovery}</p>
    </>}
    <Button type="button" variant="outline" disabled={pending} onClick={() => { setError(""); setRefresh((value) => value + 1); }}>{text.refreshQuery}</Button>
  </section>;
}
