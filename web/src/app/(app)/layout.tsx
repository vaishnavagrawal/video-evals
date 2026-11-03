import { Suspense } from "react";
import { AppShell } from "@/components/app-shell";

function ShellFallback() {
  return (
    <div className="min-h-full flex flex-col">
      <header className="border-b border-line bg-subtle h-12" />
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
