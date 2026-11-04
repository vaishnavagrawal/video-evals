import { Suspense } from "react";
import { connection } from "next/server";
import { FilmSlateIcon } from "@phosphor-icons/react/dist/ssr";
import { isSignUpAllowed } from "@/lib/auth";
import { LoginForm } from "./login-form";

async function LoginPanel() {
  // Wait for the request so ALLOW_SIGNUP is read at runtime. Without this the page is prerendered at build time
  // and the flag would be frozen to whatever the build environment had.
  await connection();
  return <LoginForm signUpAllowed={isSignUpAllowed()} />;
}

export default function LoginPage() {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-6 bg-background p-6 md:p-10">
      <div className="flex w-full max-w-sm flex-col gap-6">
        <div className="flex items-center gap-2 self-center font-medium">
          <span className="flex size-7 items-center justify-center bg-primary text-primary-foreground">
            <FilmSlateIcon className="size-4" weight="bold" />
          </span>
          <span className="text-lg font-semibold">vreview</span>
        </div>
        <Suspense>
          <LoginPanel />
        </Suspense>
        <p className="text-center text-xs text-muted-foreground">Review dubbed and AI-generated video like code.</p>
      </div>
    </div>
  );
}
