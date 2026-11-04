import { Suspense } from "react";
import { AppShell } from "@/components/app-shell";

function ShellFallback() {
  return (
    <div className="flex min-h-svh">
      <div className="hidden w-64 shrink-0 border-r bg-sidebar md:block" />
      <div className="flex-1">
        <header className="h-12 border-b bg-card" />
      </div>
    </div>
  );
}

export default function AppLayout({ children }: { children: React.ReactNode }) {
  // The shell reads the session and pathname at runtime, so it streams in after the static prerender.
  return (
    <Suspense fallback={<ShellFallback />}>
      <AppShell>{children}</AppShell>
    </Suspense>
  );
}
