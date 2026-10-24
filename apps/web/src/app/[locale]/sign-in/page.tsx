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
      </div>
      <div className="w-full md:max-w-sm"><SignIn locale={locale} /></div>
    </main>
  );
}
