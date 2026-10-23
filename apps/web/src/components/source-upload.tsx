"use client";

import { useEffect, useRef, useState } from "react";

import { SourceViewer } from "@/components/source-viewer";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { apiRequest } from "@/lib/api/client";
import type { components } from "@/lib/api/generated";
import { messages, type Locale } from "@/lib/i18n";

type Result = components["schemas"]["SourceResult"];
type Page = components["schemas"]["SourcePageList"];

export function SourceUpload({ business, locale, onChanged }: {
  business: string;
  locale: Locale;
  onChanged: () => void;
}) {
  const text = messages[locale];
  const [page, setPage] = useState<Page | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [content, setContent] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const root = `/v1/businesses/${business}/sources` as const;

  useEffect(() => {
    const controller = new AbortController();
    apiRequest<Page>(`${root}${cursor ? `?cursor=${cursor}` : ""}`, { signal: controller.signal })
      .then((value) => { if (!controller.signal.aborted) setPage(value); })
      .catch(() => { if (!controller.signal.aborted) setError(text.unavailable); });
    return () => controller.abort();
  }, [root, cursor, refresh, text.unavailable]);

  async function upload(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    if ((!file && !content.trim()) || (file && content.trim())) { setError(text.chooseSource); return; }
    if (file && file.size > 10 * 1024 * 1024) { setError(text.sourceLimits); return; }
    const body = new FormData();
    if (file) body.set("file", file); else body.set("text", content);
    setPending(true);
    try {
      const result = await apiRequest<Result>(root, {
        method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() }, body,
      });
      setSelected(result.source.id);
      setFile(null); setContent("");
      if (input.current) input.current.value = "";
      setCursor(null); setRefresh((value) => value + 1);
      onChanged();
    } catch { setError(text.sourceUploadFailed); }
    finally { setPending(false); }
  }

  return (
    <section className="space-y-4 rounded-xl border bg-card p-5" aria-label={text.sources}>
      <h2 className="font-serif text-2xl">{text.sources}</h2>
      <p className="text-sm text-muted-foreground">{text.sourceLimits}</p>
      {error && <p role="alert" className="text-destructive">{error}</p>}
      <form className="space-y-3" onSubmit={upload}>
        <label className="block text-sm">{text.sourceFile}<Input ref={input} className="mt-2" type="file" accept="image/png,image/jpeg,application/pdf,text/plain" disabled={pending || Boolean(content)} onChange={(event) => setFile(event.target.files?.[0] ?? null)} /></label>
        <label className="block text-sm">{text.sourceText}<textarea className="mt-2 min-h-24 w-full rounded border bg-card p-3" maxLength={16000} disabled={pending || Boolean(file)} value={content} onChange={(event) => setContent(event.target.value)} /></label>
        <Button type="submit" disabled={pending}>{pending ? text.uploadingSource : text.uploadSource}</Button>
      </form>
      {pending && <p role="status" className="text-sm">{text.uploadingSource}</p>}
      {page?.items.length === 0 && <p className="text-sm">{text.noSources}</p>}
      <ul className="space-y-2">
        {page?.items.map((source) => <li key={source.id} className="flex flex-wrap items-center justify-between gap-3 rounded border p-3 text-sm">
          <div><p>{source.kind} · {text[`source_${source.status}`]}</p><p className="break-all text-xs text-muted-foreground">{source.id}</p></div>
          <Button type="button" variant="outline" disabled={source.status !== "available"} onClick={() => setSelected(source.id)}>{text.viewSource}</Button>
        </li>)}
      </ul>
      {page?.next_cursor && <Button variant="outline" onClick={() => setCursor(page.next_cursor ?? null)}>{text.nextPage}</Button>}
      {selected && <SourceViewer key={selected} business={business} sourceId={selected} locale={locale} onPrepared={onChanged} />}
    </section>
  );
}
