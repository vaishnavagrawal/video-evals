import { Suspense } from "react";
import { ReviewPage } from "@/components/review/review-page";

async function WithRunId({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = await params;
  const id = decodeURIComponent(runId);
  return <ReviewPage key={id} runId={id} />;
}

export default function Page({ params }: PageProps<"/reviews/[runId]">) {
  return (
    <Suspense fallback={<div className="mx-auto max-w-[1440px] px-4 py-5 text-muted-foreground">Loading review…</div>}>
      <WithRunId params={params} />
    </Suspense>
  );
}
