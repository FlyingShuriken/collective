"use client";

import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { components } from "@/lib/api/generated";
import { messages, type Locale } from "@/lib/i18n";

type Reading = components["schemas"]["IntakeReading"];
type Context = components["schemas"]["RecordContext"];
type Options = components["schemas"]["RecordOptions"];
type Line = components["schemas"]["InvoiceLineReading"];
const selectClass = "mt-2 min-h-11 w-full rounded-md border bg-card px-3 text-sm";

export function ProposalEditor({ proposal, options, locale, pending, onSave, onCancel }: {
  proposal: components["schemas"]["DocumentProposal"];
  options: Options;
  locale: Locale;
  pending: boolean;
  onSave: (body: components["schemas"]["EditDocumentProposal"]) => void;
  onCancel: () => void;
}) {
  const text = messages[locale].records;
  const [reading, setReading] = useState(proposal.reading);
  const [context, setContext] = useState<Context>(proposal.context);
  const purchase = reading.kind === "purchase";

  function change<K extends keyof Reading>(key: K, value: Reading[K]) {
    setReading((current) => ({ ...current, [key]: value,
      issues: current.issues?.filter((issue) => issue.field_path !== key && !issue.field_path.startsWith(`${key}.`)),
    }));
  }

  function line(index: number, key: keyof Line, value: string) {
    setReading((current) => ({ ...current,
      lines: current.lines?.map((item, i) => i === index ? { ...item, [key]: value || null } : item),
      issues: current.issues?.filter((issue) => issue.field_path !== `lines.${index}.${key}`),
    }));
  }

  function field(key: keyof typeof text.fields, value: string | number | null | undefined, onChange: (value: string) => void, type = "text", decimal = false) {
    return <label key={key} className="block text-sm">{text.fields[key]}
      <Input className="mt-2" type={type} step={type === "datetime-local" ? "1" : undefined} value={value ?? ""} maxLength={2000}
        inputMode={decimal ? "decimal" : undefined} pattern={decimal ? "[0-9]+([.][0-9]{1,6})?" : undefined}
        onChange={(event) => onChange(event.target.value)} />
    </label>;
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onSave({ expected_revision: proposal.current_revision, reading, context });
  }

  const quantities = ["quantity", "unit_price", "line_total"];
  const charges = ["goods_total", "delivery_fee", "tax_amount", "tax_rate", "discount", "surcharge", "total"] as const;
  return <form onSubmit={submit} className="space-y-5" aria-label={text.editRecord}>
    <p className="text-sm text-muted-foreground">{text.blankMeansUnknown}</p>
    <fieldset disabled={pending} className="space-y-5 disabled:opacity-60">
      <label className="block text-sm">{text.recordType}
        <select className={selectClass} value={reading.kind} onChange={(event) => {
          change("kind", event.target.value as "purchase" | "stock");
          setContext({ supplier_id: context.supplier_id, purpose: context.purpose, line_mappings: [], stock_mapping: null, stock_kind: "stock", po_id: null });
        }}>
          {!["purchase", "stock"].includes(reading.kind) && <option value={reading.kind}>{text.chooseType}</option>}
          <option value="purchase">{text.purchase}</option><option value="stock">{text.stock}</option>
        </select>
      </label>
      {purchase ? <>
        <div className="grid gap-4 sm:grid-cols-2">
          {field("supplier_name", reading.supplier_name, (value) => change("supplier_name", value || null))}
          <label className="block text-sm">{text.confirmedSupplier}<select className={selectClass} required value={context.supplier_id ?? ""} onChange={(event) => setContext({ ...context, supplier_id: event.target.value || null, line_mappings: [] })}>
            <option value="">{text.chooseSupplier}</option>
            {options.suppliers.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select></label>
          <label className="block text-sm">{text.purpose}<select className={selectClass} required value={context.purpose ?? ""} onChange={(event) => setContext({ ...context, purpose: event.target.value as Context["purpose"] })}>
            <option value="">{text.choosePurpose}</option><option value="historical">{text.historical}</option><option value="delivered">{text.delivered}</option>
          </select></label>
          {field("document_number", reading.document_number, (value) => change("document_number", value || null))}
          {field("date_text", reading.date_text, (value) => change("date_text", value || null))}
          {field("document_date", reading.document_date, (value) => change("document_date", value || null), "date")}
          {field("currency", reading.currency, (value) => change("currency", value || null))}
        </div>
        <section className="space-y-3" aria-label={text.lines}>
          <h4 className="font-semibold">{text.lines}</h4>
          {reading.lines?.map((item, index) => <fieldset key={index} className="space-y-3 rounded-lg border p-3">
            <legend className="px-1 text-sm font-semibold">{text.line} {index + 1}</legend>
            <div className="grid gap-3 sm:grid-cols-2">
              {(["description", "supplier_sku", "quantity", "unit", "unit_price", "line_total"] as const).map((key) => field(key, item[key], (value) => line(index, key, value), "text", quantities.includes(key)))}
              <label className="block text-sm">{text.mapping}<select className={selectClass} value={context.line_mappings?.find((selected) => selected.line === index)?.id ?? ""} onChange={(event) => {
                const selected = options.mappings.find((mapping) => mapping.id === event.target.value);
                setContext({ ...context, line_mappings: [...(context.line_mappings ?? []).filter((mapping) => mapping.line !== index), ...(selected ? [{ line: index, id: selected.id, revision: selected.revision }] : [])] });
              }}>
                <option value="">{text.unmapped}</option>
                {options.mappings.filter((mapping) => mapping.supplier_id === context.supplier_id).map((mapping) => <option key={mapping.id} value={mapping.id}>{mapping.supplier_sku} · {mapping.spec_name}</option>)}
              </select></label>
            </div>
            <Button type="button" variant="outline" onClick={() => {
              change("lines", reading.lines?.filter((_, i) => i !== index) ?? []);
              setContext({ ...context, line_mappings: (context.line_mappings ?? []).filter((m) => m.line !== index).map((m) => m.line > index ? { ...m, line: m.line - 1 } : m) });
            }}>{text.removeLine}</Button>
          </fieldset>)}
          <Button type="button" variant="outline" disabled={(reading.lines?.length ?? 0) >= 100} onClick={() => change("lines", [...(reading.lines ?? []), { description: null, supplier_sku: null, quantity: null, unit: null, unit_price: null, line_total: null }])}>{text.addLine}</Button>
          <p className="text-sm text-muted-foreground">{text.mappingExplanation}</p>
        </section>
        <fieldset className="space-y-3 border-t pt-4">
          <legend className="font-semibold">{text.charges}</legend>
          <div className="grid gap-4 sm:grid-cols-2">
            {charges.map((key) => field(key, reading[key], (value) => change(key, value || null), "text", true))}
            <label className="block text-sm">{text.fields.credit_days}<Input className="mt-2" type="number" min={0} max={3650} step={1} value={reading.credit_days ?? ""} onChange={(event) => change("credit_days", event.target.value === "" ? null : Number(event.target.value))} /></label>
            <label className="block text-sm">{text.fields.delivery_mode}<select className={selectClass} value={reading.delivery_mode ?? ""} onChange={(event) => change("delivery_mode", (event.target.value || null) as Reading["delivery_mode"])}>
              <option value="">{text.unknown}</option><option value="per_business">{text.perBusiness}</option><option value="collection_point">{text.collectionPoint}</option>
            </select></label>
          </div>
          {reading.additional_fees?.map((fee, index) => <div key={index} className="space-y-3 rounded border p-3">
            <div className="grid gap-3 sm:grid-cols-2">
              {field("fee_label", fee.label, (value) => change("additional_fees", reading.additional_fees?.map((f, i) => i === index ? { ...f, label: value || null } : f) ?? []))}
              {field("fee_amount", fee.amount, (value) => change("additional_fees", reading.additional_fees?.map((f, i) => i === index ? { ...f, amount: value || null } : f) ?? []), "text", true)}
            </div>
            <Button type="button" variant="outline" onClick={() => change("additional_fees", reading.additional_fees?.filter((_, i) => i !== index) ?? [])}>{text.removeFee}</Button>
          </div>)}
          <Button type="button" variant="outline" disabled={(reading.additional_fees?.length ?? 0) >= 30} onClick={() => change("additional_fees", [...(reading.additional_fees ?? []), { label: null, amount: null }])}>{text.addFee}</Button>
        </fieldset>
      </> : reading.kind === "stock" ? <div className="grid gap-4 sm:grid-cols-2">
        {field("stock_description", reading.stock_description, (value) => change("stock_description", value || null))}
        <label className="block text-sm">{text.mapping}<select className={selectClass} required value={context.stock_mapping?.id ?? ""} onChange={(event) => {
          const selected = options.mappings.find((mapping) => mapping.id === event.target.value);
          setContext({ ...context, stock_mapping: selected ? { id: selected.id, revision: selected.revision } : null });
        }}><option value="">{text.chooseMapping}</option>{options.mappings.map((mapping) => <option key={mapping.id} value={mapping.id}>{options.suppliers.find((s) => s.id === mapping.supplier_id)?.name} · {mapping.supplier_sku} · {mapping.spec_name}</option>)}</select></label>
        {field("stock_quantity", reading.stock_quantity, (value) => change("stock_quantity", value || null), "text", true)}
        {field("stock_unit", reading.stock_unit, (value) => change("stock_unit", value || null))}
        {field("stock_date_text", reading.stock_date_text, (value) => change("stock_date_text", value || null))}
        {field("stock_observed_at", reading.stock_observed_at ? new Date(new Date(reading.stock_observed_at).getTime() + 8 * 3600000).toISOString().slice(0, 19) : "", (value) => change("stock_observed_at", value ? new Date(`${value}+08:00`).toISOString() : null), "datetime-local")}
        {field("reported_usage_per_day", reading.reported_usage_per_day, (value) => change("reported_usage_per_day", value || null), "text", true)}
        <label className="block text-sm">{text.stockKind}<select className={selectClass} value={context.stock_kind ?? "stock"} onChange={(event) => setContext({ ...context, stock_kind: event.target.value as Context["stock_kind"], po_id: null })}>
          <option value="stock">{text.stockCount}</option><option value="receipt">{text.receipt}</option><option value="inbound_adjustment">{text.inbound}</option><option value="waste">{text.waste}</option>
        </select></label>
        <p className="text-sm text-muted-foreground sm:col-span-2">{text.usageExplanation}</p>
      </div> : <p role="status">{text.chooseType}</p>}
      <div className="flex flex-wrap gap-3 border-t pt-4">
        <Button type="submit">{pending ? text.saving : text.save}</Button>
        <Button type="button" variant="outline" onClick={onCancel}>{text.cancel}</Button>
      </div>
    </fieldset>
  </form>;
}
