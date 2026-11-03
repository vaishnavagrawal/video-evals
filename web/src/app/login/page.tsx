import { Suspense } from "react";
import { connection } from "next/server";
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
    <div className="flex-1 flex items-start justify-center pt-24 px-4 bg-subtle">
      <div className="w-full max-w-[340px]">
        <h1 className="text-center text-[20px] font-semibold mb-1">vreview</h1>
        <p className="text-center text-fg-muted mb-5">Review dubbed video like code</p>
        <Suspense>
          <LoginPanel />
        </Suspense>
      </div>
    </div>
  );
}
