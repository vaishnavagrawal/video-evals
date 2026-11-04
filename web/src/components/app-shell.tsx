"use client";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar";
import { authClient } from "@/lib/auth-client";
import { AppSidebar } from "./app-sidebar";
import { Spinner } from "./common";
import { SiteHeader } from "./site-header";

const shellVars = {
  "--sidebar-width": "calc(var(--spacing) * 64)",
  "--header-height": "calc(var(--spacing) * 12)",
} as React.CSSProperties;

export function AppShell({ children }: { children: React.ReactNode }) {
  const { data: session, isPending } = authClient.useSession();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (!isPending && !session) router.replace(`/login?next=${encodeURIComponent(pathname)}`);
  }, [isPending, session, router, pathname]);

  const user = session
    ? { name: session.user.name || session.user.email.split("@")[0], email: session.user.email, image: session.user.image }
    : { name: "…", email: "" };

  return (
    <SidebarProvider style={shellVars}>
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 z-50 bg-card px-2 py-1 border"
      >
        Skip to content
      </a>
      <AppSidebar user={user} />
      <SidebarInset className="min-w-0">
        <SiteHeader />
        <main id="main" className="flex flex-1 flex-col">
          {/* The proxy already redirected requests without a session cookie; an expired session redirects above. */}
          {!isPending && !session ? (
            <div className="p-8">
              <Spinner label="Redirecting to sign in" />
            </div>
          ) : (
            children
          )}
        </main>
      </SidebarInset>
    </SidebarProvider>
  );
}
