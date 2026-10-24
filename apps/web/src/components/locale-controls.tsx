"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import { Pwa } from "@/components/pwa";
import { locales, messages, type Locale } from "@/lib/i18n";

export function LocaleControls({ locale }: { locale: Locale }) {
  const path = usePathname();
  const router = useRouter();
  useEffect(() => { document.documentElement.lang = locale; }, [locale]);
  return <div className="mx-auto flex w-full max-w-5xl flex-wrap items-center justify-end gap-3 px-5 pt-4 sm:px-8">
    <nav aria-label={messages[locale].language} className="flex flex-wrap gap-1">
      {locales.map((value) => <a key={value} lang={value} href={`/${value}${path.slice(3)}`}
        aria-current={locale === value ? "true" : undefined}
        onClick={(event) => { event.preventDefault(); router.push(`/${value}${path.slice(3)}${window.location.search}`); }}
        className={`inline-flex min-h-11 items-center rounded-md px-3 text-sm ${locale === value ? "font-semibold underline underline-offset-4" : ""}`}>{({ en: "English", ms: "Bahasa Melayu", zh: "中文" })[value]}</a>)}
    </nav>
    <Pwa locale={locale} />
  </div>;
}
