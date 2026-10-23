import en from "@/messages/en.json";
import ms from "@/messages/ms.json";
import zh from "@/messages/zh.json";

export const locales = ["en", "ms", "zh"] as const;
export type Locale = typeof locales[number];
export function isLocale(value: string): value is Locale {
  return locales.some((locale) => locale === value);
}
export const messages = { en, ms, zh };
