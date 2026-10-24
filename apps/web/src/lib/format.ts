import type { Locale } from "@/lib/i18n";

export function money(value: string | null | undefined) {
  if (value == null || !/^-?\d+(\.\d+)?$/.test(value)) return "—";
  const [whole, fraction = ""] = value.split(".");
  return `RM ${whole}.${fraction.padEnd(2, "0")}`;
}

export function date(value: string, locale: Locale, withTime = false) {
  return new Intl.DateTimeFormat({ en: "en-MY", ms: "ms-MY", zh: "zh-MY" }[locale], {
    dateStyle: "medium", timeStyle: withTime ? "short" : undefined,
    timeZone: "Asia/Kuala_Lumpur",
  }).format(new Date(value));
}

export function decimal(value: string) {
  return value.includes(".") ? value.replace(/0+$/, "").replace(/\.$/, "") : value;
}
