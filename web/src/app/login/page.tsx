import { Suspense } from "react";
import { LoginForm } from "./login-form";

export default function LoginPage() {
  return (
    <div className="flex-1 flex items-start justify-center pt-24 px-4 bg-subtle">
      <div className="w-full max-w-[340px]">
        <h1 className="text-center text-[20px] font-semibold mb-1">vreview</h1>
        <p className="text-center text-fg-muted mb-5">Review dubbed video like code</p>
        <Suspense>
          <LoginForm />
        </Suspense>
      </div>
    </div>
  );
}
