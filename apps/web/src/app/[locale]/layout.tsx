import { notFound } from "next/navigation";
import { isLocale } from "@/lib/i18n";
import { LocaleControls } from "@/components/locale-controls";

export default async function LocaleLayout({ children, params }: LayoutProps<"/[locale]">) {
  const { locale } = await params;
  if (!isLocale(locale)) notFound();
  return <div lang={locale}><LocaleControls locale={locale} />{children}</div>;
}
