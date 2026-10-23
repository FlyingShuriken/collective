"use client";

import Image from "next/image";
import { useEffect, useState, type PointerEvent } from "react";

import { Button } from "@/components/ui/button";
import { apiRequest } from "@/lib/api/client";
import type { components } from "@/lib/api/generated";
import { messages, type Locale } from "@/lib/i18n";

type Result = components["schemas"]["SourceResult"];
type Region = components["schemas"]["MaskRegion"];
type Evidence = components["schemas"]["Evidence"];

export function SourceViewer({ business, sourceId, locale, evidence, onPrepared }: {
  business: string;
  sourceId: string;
  locale: Locale;
  evidence?: Evidence;
  onPrepared?: () => void;
}) {
  const text = messages[locale];
  const [result, setResult] = useState<Result | null>(null);
  const [page, setPage] = useState(evidence?.page ?? 1);
  const [content, setContent] = useState("");
  const [provider, setProvider] = useState(false);
  const [regions, setRegions] = useState<Region[]>([]);
  const [adding, setAdding] = useState(false);
  const [drag, setDrag] = useState<number[] | null>(null);
  const [crop, setCrop] = useState<number[] | null>(null);
  const [sensitive, setSensitive] = useState("");
  const [reviewed, setReviewed] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const root = `/v1/businesses/${business}/sources/${sourceId}` as const;
  const source = result?.source;
  const visible = source?.price_visibility === "visible";
  const raster = source?.kind === "image" || source?.kind === "pdf";
  const ownPage = visible ? source.pages.find((item) => item.page === page) : null;
  const view = `/api${root}/view?page=${page}&variant=${provider ? "provider" : "page"}`;

  useEffect(() => {
    const controller = new AbortController();
    apiRequest<Result>(root, { signal: controller.signal })
      .then((value) => { if (!controller.signal.aborted) setResult(value); })
      .catch(() => { if (!controller.signal.aborted) setError(text.unavailable); });
    return () => controller.abort();
  }, [root, text.unavailable]);

  useEffect(() => {
    if (!visible || raster || source.status !== "available") return;
    const controller = new AbortController();
    fetch(view, { signal: controller.signal, cache: "no-store" })
      .then(async (response) => { if (!response.ok) throw new Error(); return response.text(); })
      .then((value) => { if (!controller.signal.aborted) setContent(value); })
      .catch(() => { if (!controller.signal.aborted) setError(text.unavailable); });
    return () => controller.abort();
  }, [visible, raster, source?.status, view, text.unavailable]);

  function point(event: PointerEvent<HTMLDivElement>) {
    const box = event.currentTarget.getBoundingClientRect();
    return [Math.round(Math.max(0, Math.min(1, (event.clientX - box.left) / box.width)) * 1000),
      Math.round(Math.max(0, Math.min(1, (event.clientY - box.top) / box.height)) * 1000)];
  }

  async function prepare(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!visible || !source.sha256) return;
    setPending(true);
    setError("");
    try {
      setResult(await apiRequest<Result>(`${root}/prepare`, {
        method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ source_sha256: source.sha256, regions,
          sensitive_text: sensitive.split("\n").filter((value) => value.trim()), reviewed }),
      }));
      setProvider(true);
      setCrop(null);
      onPrepared?.();
    } catch { setError(text.sourcePrepareFailed); }
    finally { setPending(false); }
  }

  const anchorMatches = !evidence || evidence.source_id === sourceId;
  const evidenceBox = anchorMatches && evidence?.page === page ? evidence.bbox_normalized : null;
  const selected = crop ?? evidenceBox;
  const cropQuery = selected ? selected.map((value) => `bbox=${value}`).join("&") : "";
  const spans = anchorMatches && evidence?.text_span && evidence.region_quality === "text" ? evidence.text_span : null;
  const characters = Array.from(content);
  const validSpan = spans && spans[0] >= 0 && spans[1] <= characters.length;
  return (
    <section className="space-y-4 rounded-lg border p-4" aria-label={text.sourceViewer}>
      <h3 className="font-semibold">{text.sourceViewer}</h3>
      {error && <p role="alert" className="text-destructive">{error}</p>}
      {!result && !error && <p role="status">{text.loading}</p>}
      {source?.price_visibility === "hidden" && <p>{text.sourceRestricted}</p>}
      {visible && source.status === "available" && <>
        <p className="break-all text-xs text-muted-foreground">{text.sourceReference}: {sourceId}</p>
        {!anchorMatches && <p role="alert">{text.evidenceUnavailable}</p>}
        {source.preparation_status === "ready" && <Button variant="outline" onClick={() => { setProvider((value) => !value); setCrop(null); }}>{provider ? text.showOriginal : text.showPrepared}</Button>}
        <p className="text-sm">{provider ? text.preparedCopy : text.privateOriginal}</p>
        {raster && ownPage ? <>
          <label className="block text-sm">{text.sourcePage}
            <select className="ml-3 rounded border bg-card p-2" value={page} onChange={(event) => { setPage(Number(event.target.value)); setCrop(null); }}>
              {source.pages.map((item) => <option key={item.page} value={item.page}>{item.page}</option>)}
            </select>
          </label>
          {source.preparation_status === "pending_review" && <div className="flex flex-wrap gap-2">
            <Button type="button" variant="outline" disabled={pending || regions.length >= 100} aria-pressed={adding} onClick={() => setAdding((value) => !value)}>{text.addMask}</Button>
            <Button type="button" variant="outline" disabled={!regions.length || pending} onClick={() => { setRegions([]); setCrop(null); }}>{text.clearMasks}</Button>
          </div>}
          {adding && <p role="status" className="text-sm">{text.drawMask}</p>}
          <div className="relative overflow-hidden rounded border" style={{ touchAction: adding ? "none" : "auto" }}
            onPointerDown={(event) => { if (!adding) return; const p = point(event); setDrag([...p, ...p]); event.currentTarget.setPointerCapture(event.pointerId); }}
            onPointerMove={(event) => { if (drag) setDrag([drag[0], drag[1], ...point(event)]); }}
            onPointerCancel={() => { setDrag(null); setAdding(false); }}
            onPointerUp={(event) => {
              if (!drag) return;
              const end = point(event);
              const box: Region["bbox"] = [Math.min(drag[0], end[0]), Math.min(drag[1], end[1]), Math.max(drag[0], end[0]), Math.max(drag[1], end[1])];
              if (box[0] < box[2] && box[1] < box[3]) setRegions((current) => [...current, { page, bbox: box }]);
              setDrag(null); setAdding(false);
            }}>
            <Image unoptimized src={view} width={ownPage.width} height={ownPage.height} alt={text.sourcePageImage} draggable={false} className="block h-auto w-full" onError={() => setError(text.unavailable)} />
            {!provider && regions.filter((region) => region.page === page).map((region, index) => <div key={index} className="pointer-events-none absolute bg-black" style={{ left: `${region.bbox[0] / 10}%`, top: `${region.bbox[1] / 10}%`, width: `${(region.bbox[2] - region.bbox[0]) / 10}%`, height: `${(region.bbox[3] - region.bbox[1]) / 10}%` }} />)}
            {drag && <div className="pointer-events-none absolute border-2 border-primary bg-primary/20" style={{ left: `${Math.min(drag[0], drag[2]) / 10}%`, top: `${Math.min(drag[1], drag[3]) / 10}%`, width: `${Math.abs(drag[2] - drag[0]) / 10}%`, height: `${Math.abs(drag[3] - drag[1]) / 10}%` }} />}
          </div>
          {regions.filter((region) => region.page === page).map((region, index) => <Button key={index} type="button" variant="outline" onClick={() => setCrop(region.bbox)}>{text.inspectRegion} {index + 1}</Button>)}
          {selected && <section className="space-y-2" aria-label={text.sourceCrop}>
            <h4 className="text-sm font-semibold">{text.sourceCrop}</h4>
            <p className="text-sm">{evidenceBox && evidence?.region_quality === "approximate" ? text.approximateRegion : text.selectedRegion}</p>
            <Image unoptimized src={`${view}&${cropQuery}`} width={Math.max(1, Math.ceil((selected[2] - selected[0]) * ownPage.width / 1000))} height={Math.max(1, Math.ceil((selected[3] - selected[1]) * ownPage.height / 1000))} alt={text.sourceCrop} className="h-auto w-full max-w-sm border" />
          </section>}
          {evidence && !evidenceBox && <p className="text-sm">{text.fullPageFallback}</p>}
        </> : !raster ? <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words rounded bg-muted p-3 text-sm">
          {validSpan ? <>{characters.slice(0, spans[0]).join("")}<mark>{characters.slice(spans[0], spans[1]).join("")}</mark>{characters.slice(spans[1]).join("")}</> : content}
        </pre> : <p>{text.sourceOriginalOnly}</p>}
        <a href={`/api${root}/view?variant=original`} className="inline-block text-sm underline underline-offset-4">{text.downloadOriginal}</a>
        {source.preparation_status === "pending_review" && <form onSubmit={prepare} className="space-y-3 border-t pt-4">
          <p className="text-sm">{text.maskingExplanation}</p>
          {!raster && <label className="block text-sm">{text.sensitiveValues}<textarea className="mt-2 min-h-24 w-full rounded border bg-card p-3" value={sensitive} onChange={(event) => setSensitive(event.target.value)} /></label>}
          <label className="flex items-start gap-2 text-sm"><input className="mt-1" type="checkbox" required checked={reviewed} onChange={(event) => setReviewed(event.target.checked)} />{text.reviewedMasking}</label>
          <Button type="submit" disabled={pending || !reviewed}>{pending ? text.preparingSource : text.prepareSource}</Button>
        </form>}
        {source.preparation_status === "ready" && <p role="status" className="text-sm">{text.sourcePrepared}</p>}
      </>}
    </section>
  );
}
