import { notFound } from "next/navigation";
import { Approvals } from "@/components/approvals";
import { isLocale, locales } from "@/lib/i18n";

export function generateStaticParams() {
  return locales.map((locale) => ({ locale }));
}

export default async function Page({ params }: PageProps<"/[locale]/activity">) {
  const { locale } = await params;
  if (!isLocale(locale)) notFound();
  return <Approvals locale={locale} screen="activity" />;
}
