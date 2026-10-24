"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { messages, type Locale } from "@/lib/i18n";

type InstallPrompt = Event & { prompt: () => Promise<void> };

export function Pwa({ locale }: { locale: Locale }) {
  const [prompt, setPrompt] = useState<InstallPrompt | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!("serviceWorker" in navigator)) return;
    let active = true;
    navigator.serviceWorker.register("/sw.js", { scope: "/", updateViaCache: "none" })
      .catch(() => { if (active) setFailed(true); });
    function available(event: Event) { event.preventDefault(); setPrompt(event as InstallPrompt); }
    function installed() { setPrompt(null); }
    window.addEventListener("beforeinstallprompt", available);
    window.addEventListener("appinstalled", installed);
    return () => {
      active = false;
      window.removeEventListener("beforeinstallprompt", available);
      window.removeEventListener("appinstalled", installed);
    };
  }, []);
  if (failed) return <p role="status" className="text-xs">{messages[locale].pwa.unavailable}</p>;
  return prompt && <Button variant="outline" onClick={() => { void prompt.prompt().catch(() => setFailed(true)); setPrompt(null); }}>{messages[locale].pwa.install}</Button>;
}
