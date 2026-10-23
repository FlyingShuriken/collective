import { notFound } from "next/navigation";

import { SignIn } from "@/components/sign-in";
import { isLocale, locales, messages } from "@/lib/i18n";

export function generateStaticParams() {
  return locales.map((locale) => ({ locale }));
}

export default async function SignInPage({ params }: PageProps<"/[locale]/sign-in">) {
  const { locale } = await params;
  if (!isLocale(locale)) notFound();
  return (
    <main lang={locale} className="mx-auto flex min-h-screen w-full max-w-5xl flex-col justify-center gap-10 px-6 py-10 md:flex-row md:items-center md:gap-20">
      <div className="md:flex-1">
        <p className="text-xs font-semibold tracking-[0.18em] uppercase">Orbyte Collective</p>
        <p className="mt-5 max-w-md font-serif text-4xl leading-tight sm:text-5xl">
          {messages[locale].intro}
        </p>
        <nav aria-label="Language" className="mt-6 flex gap-4 text-sm">
          <a href="/en/sign-in" lang="en" hrefLang="en" aria-current={locale === "en" ? "page" : undefined}>
            English
          </a>
          <a href="/ms/sign-in" lang="ms" hrefLang="ms" aria-current={locale === "ms" ? "page" : undefined}>
            Bahasa Melayu
          </a>
          <a href="/zh/sign-in" lang="zh" hrefLang="zh" aria-current={locale === "zh" ? "page" : undefined}>
            中文
          </a>
        </nav>
      </div>
      <div className="w-full md:max-w-sm"><SignIn locale={locale} /></div>
    </main>
  );
}
