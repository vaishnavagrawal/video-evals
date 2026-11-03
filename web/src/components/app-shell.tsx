"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import { authClient, clearApiToken } from "@/lib/auth-client";
import { IS_MOCK } from "@/lib/api";
import { Spinner, btn, cx } from "./ui";

export function AppShell({ children }: { children: React.ReactNode }) {
  const { data: session, isPending } = authClient.useSession();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (!isPending && !session) router.replace(`/login?next=${encodeURIComponent(pathname)}`);
  }, [isPending, session, router, pathname]);

  const nav = [
    { href: "/", label: "Reviews", active: pathname === "/" },
    { href: "/reviews/new", label: "New review", active: pathname === "/reviews/new" },
  ];

  return (
    <div className="min-h-full flex flex-col">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 bg-canvas px-2 py-1 rounded border border-line z-50">
        Skip to content
      </a>
      <header className="border-b border-line bg-subtle">
        <div className="mx-auto max-w-[1440px] px-4 h-12 flex items-center gap-4">
          <Link href="/" className="font-semibold tracking-tight text-[15px]">
            vreview
          </Link>
          <nav aria-label="Main" className="flex items-center gap-1">
            {nav.map((n) => (
              <Link
                key={n.href}
                href={n.href}
                aria-current={n.active ? "page" : undefined}
                className={cx(
                  "px-2.5 h-7 inline-flex items-center rounded-md text-[13px]",
                  n.active ? "bg-canvas border border-line font-medium" : "text-fg-muted hover:text-fg hover:bg-muted",
                )}
              >
                {n.label}
              </Link>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-3 text-[13px]">
            {IS_MOCK && (
              <span
                className="rounded border border-shouldfix/40 bg-shouldfix-soft text-shouldfix px-1.5 h-5 inline-flex items-center text-[11px] font-medium"
                title="NEXT_PUBLIC_VREVIEW_MOCK=1: review data comes from a local fixture, not the vreview API. Sign-in is real."
              >
                Mock API
              </span>
            )}
            {session && <span className="text-fg-muted hidden sm:inline">{session.user.email}</span>}
            {session && (
              <button
                type="button"
                className={cx(btn.base, btn.default, btn.small)}
                onClick={async () => {
                  clearApiToken();
                  await authClient.signOut();
                  router.replace("/login");
                }}
              >
                Sign out
              </button>
            )}
          </div>
        </div>
      </header>
      <main id="main" className="flex-1">
        {/* The proxy already redirected requests without a session cookie; an expired session redirects above. */}
        {!isPending && !session ? (
          <div className="p-8">
            <Spinner label="Redirecting to sign in" />
          </div>
        ) : (
          children
        )}
      </main>
    </div>
  );
}
