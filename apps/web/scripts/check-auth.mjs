import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createServerClient } from "@supabase/ssr";

const seed = JSON.parse(await readFile(new URL("../../../.cache/demo-demo.json", import.meta.url)));
const origin = process.env.COLLECTIVE_WEB_URL ?? "http://127.0.0.1:3000";
assert.equal(new URL(origin).hostname, "127.0.0.1", "Auth checks require the disposable local web server");
assert.equal(new URL(process.env.NEXT_PUBLIC_SUPABASE_URL).hostname, "127.0.0.1", "Auth checks require local Supabase");

async function signIn(name) {
  const jar = new Map();
  const supabase = createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL,
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY,
    { cookies: {
      getAll: () => [...jar].map(([name, value]) => ({ name, value })),
      setAll: (values) => values.forEach(({ name, value }) => jar.set(name, value)),
    } },
  );
  const account = seed.accounts[name];
  const { error } = await supabase.auth.signInWithPassword({ email: account.email, password: account.password });
  assert.equal(error, null, "Seeded sign-in must succeed");
  return { jar, subject: account.id };
}

function cookie(jar) {
  return [...jar].filter(([, value]) => value).map(([name, value]) => `${name}=${value}`).join("; ");
}

async function identity(jar, expectedSubject, role) {
  const response = await fetch(origin + "/api/v1/me", { headers: { cookie: cookie(jar) } });
  assert.equal(response.status, 200);
  assert.match(response.headers.get("cache-control"), /no-store/);
  const value = await response.json();
  assert.equal(value.subject, expectedSubject);
  assert.deepEqual(value.memberships.map((item) => item.role), [role]);
  return response;
}

const owner = await signIn("owner-a");
const staff = await signIn("staff");
await identity(owner.jar, owner.subject, "owner");
await identity(staff.jar, staff.subject, "staff");

const anonymous = await fetch(origin + "/api/v1/me");
assert.equal(anonymous.status, 401);
assert.equal((await anonymous.json()).code, "UNAUTHENTICATED");
const spoofed = await fetch(origin + "/api/v1/me", {
  headers: { cookie: cookie(staff.jar), authorization: "Bearer forged-owner-token" },
});
assert.equal((await spoofed.json()).subject, staff.subject);
for (const untrustedOrigin of [null, "https://untrusted.example"]) {
  const headers = { cookie: cookie(owner.jar) };
  if (untrustedOrigin) headers.origin = untrustedOrigin;
  const denied = await fetch(origin + "/api/v1/me", { method: "POST", headers });
  assert.equal(denied.status, 403);
}
const forwarded = await fetch(origin + "/api/v1/me", {
  method: "POST", headers: { cookie: cookie(owner.jar), origin },
});
assert.equal(forwarded.status, 405);
assert.equal((await forwarded.json()).code, "METHOD_NOT_ALLOWED");
const unknown = await fetch(origin + "/api/v1/unconfigured", { headers: { cookie: cookie(owner.jar) } });
assert.equal(unknown.status, 404);

// Exercise the pinned SSR cookie format with a session that needs refreshing.
const names = [...owner.jar.keys()].filter((name) => name.includes("auth-token"));
const root = names[0].replace(/\.\d+$/, "");
const encoded = names.sort().map((name) => owner.jar.get(name)).join("");
assert.ok(encoded.startsWith("base64-"), "Expected the pinned SSR session format");
const session = JSON.parse(Buffer.from(encoded.slice(7), "base64url").toString());
session.expires_at = Math.floor(Date.now() / 1000) - 60;
names.forEach((name) => owner.jar.delete(name));
owner.jar.set(root, "base64-" + Buffer.from(JSON.stringify(session)).toString("base64url"));
const refreshed = await identity(owner.jar, owner.subject, "owner");
assert.ok(refreshed.headers.getSetCookie().some((value) => value.startsWith(root)), "SSR refresh must return the updated session cookie");

console.log("Auth proxy checks passed: owner/staff subjects, cookie refresh, private responses, forged headers and cross-origin mutation rejection.");
