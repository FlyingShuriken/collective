"use client";

import { useEffect, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError, apiRequest } from "@/lib/api/client";
import type { components } from "@/lib/api/generated";
import { messages, type Locale } from "@/lib/i18n";
import { createClient } from "@/lib/supabase/client";

type Identity = components["schemas"]["Identity"];

export function SignIn({ locale }: { locale: Locale }) {
  const text = messages[locale];
  const [identity, setIdentity] = useState<Identity | null>(null);
  const [pending, setPending] = useState(false);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    apiRequest<Identity>("/v1/me", { signal: controller.signal })
      .then(setIdentity)
      .catch((error) => {
        if (!controller.signal.aborted && (!(error instanceof ApiError) || error.status !== 401)) {
          setError(text.unavailable);
        }
      }).finally(() => { if (!controller.signal.aborted) setReady(true); });
    return () => controller.abort();
  }, [text.unavailable]);

  async function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setPending(true);
    setError("");
    try {
      const { error } = await createClient().auth.signInWithPassword({
        email: String(form.get("email")),
        password: String(form.get("password")),
      });
      if (error) {
        setError(text.failed);
        return;
      }
      setIdentity(await apiRequest<Identity>("/v1/me"));
    } catch {
      setError(text.unavailable);
    } finally {
      setPending(false);
    }
  }

  async function signOut() {
    setPending(true);
    setError("");
    try {
      const { error } = await createClient().auth.signOut({ scope: "local" });
      if (error) {
        setError(text.unavailable);
        return;
      }
      setIdentity(null);
    } catch {
      setError(text.unavailable);
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="w-full rounded-2xl border bg-card p-6 shadow-sm sm:p-9" aria-labelledby="sign-in-heading">
      <h1 id="sign-in-heading" className="font-serif text-3xl">
        {identity ? text.signedIn : text.signIn}
      </h1>
      {error && <p role="alert" className="mt-4 text-sm text-destructive">{error}</p>}
      {identity ? (
        <div className="mt-6 space-y-5">
          <p className="text-sm text-muted-foreground">{text.assigned}</p>
          <ul className="space-y-3">
            {identity.memberships.map((membership) => (
              <li key={membership.business_id} className="flex justify-between gap-3 border-b pb-3">
                <span>{membership.name}</span>
                <span className="text-muted-foreground">{text[membership.role]}</span>
              </li>
            ))}
            {identity.networks?.map((network) => (
              <li key={network.network_id}>{network.name} · {text.network_admin}</li>
            ))}
          </ul>
          {!identity.memberships.length && !identity.networks?.length && <p>{text.noMembership}</p>}
          {(identity.memberships.length > 0 || !!identity.networks?.length) && <a href={`/${locale}/approvals`} className="block text-sm underline underline-offset-4">{text.openWorkspace}</a>}
          <Button type="button" variant="outline" onClick={signOut} disabled={pending}>
            {text.signOut}
          </Button>
        </div>
      ) : (
        <form method="post" onSubmit={signIn} className="mt-7 space-y-5" aria-busy={pending || !ready}>
          <div className="space-y-2">
            <label htmlFor="email" className="text-sm font-medium">{text.email}</label>
            <Input id="email" name="email" type="email" autoComplete="username" required disabled={pending || !ready} />
          </div>
          <div className="space-y-2">
            <label htmlFor="password" className="text-sm font-medium">{text.password}</label>
            <Input id="password" name="password" type="password" autoComplete="current-password" required disabled={pending || !ready} />
          </div>
          <Button type="submit" className="w-full" disabled={pending || !ready}>
            {pending ? text.working : text.signIn}
          </Button>
        </form>
      )}
    </section>
  );
}
