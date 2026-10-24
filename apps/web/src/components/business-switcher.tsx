"use client";

import type { components } from "@/lib/api/generated";
import { messages, type Locale } from "@/lib/i18n";

export function BusinessSwitcher({ memberships, value, locale, onChange }: {
  memberships: components["schemas"]["Membership"][];
  value: string; locale: Locale; onChange: (value: string) => void;
}) {
  return <label className="block max-w-sm text-sm">{messages[locale].workspace}
    <select value={value} className="mt-2 min-h-11 w-full rounded-md border bg-card px-3"
      onChange={(event) => onChange(event.target.value)}>
      {memberships.map((item) => <option key={item.business_id} value={item.business_id}>{item.name}</option>)}
    </select>
  </label>;
}
