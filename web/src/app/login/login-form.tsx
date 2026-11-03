"use client";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";
import { authClient } from "@/lib/auth-client";
import { ErrorBox, btn, cx } from "@/components/ui";

export function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const [mode, setMode] = useState<"signin" | "signup">("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const next = params.get("next");
  const target = next && next.startsWith("/") && !next.startsWith("//") ? next : "/";

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const res =
      mode === "signin"
        ? await authClient.signIn.email({ email, password })
        : await authClient.signUp.email({ email, password, name: name || email.split("@")[0] });
    setBusy(false);
    if (res.error) setError(res.error.message ?? "Something went wrong");
    else router.replace(target);
  }

  const input = "w-full h-8 rounded-md border border-line bg-canvas px-2.5 text-[14px]";

  return (
    <form onSubmit={submit} className="rounded-md border border-line bg-canvas p-4 space-y-3">
      <div role="tablist" aria-label="Account" className="grid grid-cols-2 gap-1 rounded-md bg-muted p-0.5 text-[13px]">
        {(["signin", "signup"] as const).map((m) => (
          <button
            key={m}
            type="button"
            role="tab"
            aria-selected={mode === m}
            onClick={() => {
              setMode(m);
              setError(null);
            }}
            className={cx("h-7 rounded", mode === m ? "bg-canvas border border-line font-medium" : "text-fg-muted")}
          >
            {m === "signin" ? "Sign in" : "Create account"}
          </button>
        ))}
      </div>
      {mode === "signup" && (
        <label className="block">
          <span className="block text-[13px] font-medium mb-1">Name</span>
          <input className={input} value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" />
        </label>
      )}
      <label className="block">
        <span className="block text-[13px] font-medium mb-1">Email</span>
        <input
          className={input}
          type="email"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          autoComplete="email"
        />
      </label>
      <label className="block">
        <span className="block text-[13px] font-medium mb-1">Password</span>
        <input
          className={input}
          type="password"
          required
          minLength={8}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete={mode === "signin" ? "current-password" : "new-password"}
        />
      </label>
      {error && <ErrorBox>{error}</ErrorBox>}
      <button type="submit" disabled={busy} className={cx(btn.base, btn.primary, "w-full")}>
        {busy ? "Please wait…" : mode === "signin" ? "Sign in" : "Create account"}
      </button>
    </form>
  );
}
