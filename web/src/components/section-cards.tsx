"use client";
import type { Icon } from "@phosphor-icons/react";
import { ChartLineUpIcon, GitPullRequestIcon, ListChecksIcon, OctagonIcon } from "@phosphor-icons/react";
import { Card, CardAction, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import type { ReviewList } from "@/lib/api";
import { cx } from "./common";

type Kpi = { label: string; value: string; icon: Icon; tone: string; note: string; sub: string };

function kpis(list: ReviewList): Kpi[] {
  const items = list.items;
  const inFlight = items.filter((i) => i.status === "queued" || i.status === "running").length;
  const done = items.filter((i) => i.status === "succeeded");
  const failed = items.filter((i) => i.status === "failed").length;
  const changes = done.filter((i) => i.verdict === "request_changes").length;
  const approved = done.filter((i) => i.verdict === "approve").length;
  const blockers = done.reduce((n, i) => n + (i.counts?.blocker ?? 0), 0);
  const shouldFix = done.reduce((n, i) => n + (i.counts?.["should-fix"] ?? 0), 0);
  const scored = done.filter((i) => i.score != null);
  const avg = scored.length ? scored.reduce((n, i) => n + (i.score ?? 0), 0) / scored.length : null;
  const scope = list.total > items.length ? `latest ${items.length}` : "all reviews";
  return [
    {
      label: "Reviews",
      value: String(list.total),
      icon: ListChecksIcon,
      tone: "text-interactive",
      note: inFlight ? `${inFlight} in progress` : "None in progress",
      sub: failed ? `${failed} failed` : "No failed runs",
    },
    {
      label: "Changes requested",
      value: String(changes),
      icon: GitPullRequestIcon,
      tone: "text-shouldfix",
      note: `of ${done.length} completed`,
      sub: `${approved} approved`,
    },
    {
      label: "Open blockers",
      value: String(blockers),
      icon: OctagonIcon,
      tone: "text-blocker",
      note: `${shouldFix} should-fix threads`,
      sub: `Across ${scope}`,
    },
    {
      label: "Average score",
      value: avg == null ? "–" : avg.toFixed(1),
      icon: ChartLineUpIcon,
      tone: "text-success",
      note: "Out of 100",
      sub: `${scored.length} scored review${scored.length === 1 ? "" : "s"}`,
    },
  ];
}

export function SectionCards({ data }: { data: ReviewList | null }) {
  if (!data) {
    return (
      <div className="grid grid-cols-1 gap-4 @xl/main:grid-cols-2 @5xl/main:grid-cols-4">
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-[132px]" />
        ))}
      </div>
    );
  }
  return (
    <div className="grid grid-cols-1 gap-4 @xl/main:grid-cols-2 @5xl/main:grid-cols-4">
      {kpis(data).map((k) => (
        <Card key={k.label} className="@container/card gap-3">
          <CardHeader>
            <CardDescription>{k.label}</CardDescription>
            <CardTitle className="text-3xl font-light tabular-nums @[250px]/card:text-[2.5rem] leading-none">
              {k.value}
            </CardTitle>
            <CardAction>
              <k.icon className={cx("size-5", k.tone)} aria-hidden />
            </CardAction>
          </CardHeader>
          <CardFooter className="flex-col items-start gap-0.5 text-sm">
            <div className="font-medium">{k.note}</div>
            <div className="text-muted-foreground">{k.sub}</div>
          </CardFooter>
        </Card>
      ))}
    </div>
  );
}
