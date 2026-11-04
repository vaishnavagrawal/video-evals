"use client";
import Link from "next/link";
import { useMemo, useState } from "react";
import {
  ArrowDownIcon,
  ArrowUpIcon,
  ArrowsDownUpIcon,
  CaretLeftIcon,
  CaretRightIcon,
  MagnifyingGlassIcon,
  PlusIcon,
  TrayIcon,
} from "@phosphor-icons/react";
import {
  createColumnHelper,
  createPaginatedRowModel,
  createSortedRowModel,
  FlexRender,
  rowPaginationFeature,
  rowSortingFeature,
  sortFn_alphanumeric,
  sortFn_basic,
  tableFeatures,
  useTable,
  type PaginationState,
  type SortingState,
} from "@tanstack/react-table";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api, type ReviewList, type ReviewListItem } from "@/lib/api";
import { usePoll } from "@/lib/use-poll";
import { ErrorBox, PriorityCountsInline, StatusBadge, VerdictBadge, cx, fmtDate } from "./common";
import { SectionCards } from "./section-cards";
import { StageBar } from "./stage-progress";

const active = (l: ReviewList) => l.items.some((i) => i.status === "queued" || i.status === "running");

type View = "all" | "active" | "done" | "failed";
const VIEWS: { id: View; label: string; match: (r: ReviewListItem) => boolean }[] = [
  { id: "all", label: "All", match: () => true },
  { id: "active", label: "In progress", match: (r) => r.status === "queued" || r.status === "running" },
  { id: "done", label: "Completed", match: (r) => r.status === "succeeded" },
  { id: "failed", label: "Failed", match: (r) => r.status === "failed" },
];

const features = tableFeatures({
  rowSortingFeature,
  rowPaginationFeature,
  sortedRowModel: createSortedRowModel(),
  paginatedRowModel: createPaginatedRowModel(),
  sortFns: { basic: sortFn_basic, alphanumeric: sortFn_alphanumeric },
});
const col = createColumnHelper<typeof features, ReviewListItem>();

const nameOf = (r: ReviewListItem) => r.filenames?.[0] ?? r.run_id;

const columns = col.columns([
  col.accessor(nameOf, {
    id: "review",
    header: "Review",
    sortFn: "alphanumeric",
    cell: ({ row }) => {
      const r = row.original;
      const [candidate, ...rest] = r.filenames ?? [];
      return (
        <div className="min-w-0">
          <Link
            href={`/reviews/${encodeURIComponent(r.run_id)}`}
            className="font-medium text-foreground hover:text-link hover:underline after:absolute after:inset-0"
          >
            {candidate ?? r.run_id}
          </Link>
          <div className="text-xs text-muted-foreground truncate max-w-[200px] 2xl:max-w-[340px]">
            {rest.length ? <>vs {rest.join(", ")} · </> : null}
            <span className="font-mono">{r.run_id}</span>
          </div>
        </div>
      );
    },
  }),
  col.accessor("status", {
    header: "Status",
    sortFn: "alphanumeric",
    cell: ({ row }) => {
      const r = row.original;
      return (
        <div className="flex flex-col items-start gap-1.5">
          <StatusBadge status={r.status} />
          {(r.status === "running" || r.status === "queued" || r.status === "failed") && (
            <StageBar status={r.status} stage={r.stage} />
          )}
          {r.status === "failed" && r.error && (
            <div className="text-xs text-blocker max-w-[200px] 2xl:max-w-[300px] truncate" title={r.error}>
              {r.error}
            </div>
          )}
        </div>
      );
    },
  }),
  col.accessor((r) => r.verdict ?? undefined, {
    id: "verdict",
    header: "Verdict",
    sortFn: "alphanumeric",
    sortUndefined: "last",
    cell: ({ row }) => <VerdictBadge verdict={row.original.verdict} />,
  }),
  col.accessor((r) => r.score ?? undefined, {
    id: "score",
    header: "Score",
    sortFn: "basic",
    sortUndefined: "last",
    sortDescFirst: true,
    cell: ({ row }) => {
      const s = row.original.score;
      if (s == null) return <span className="text-muted-foreground">–</span>;
      return (
        <div className="flex items-center justify-end gap-2">
          <span className="hidden 2xl:block h-1 w-12 bg-muted" aria-hidden>
            <span className="block h-full bg-interactive" style={{ width: `${Math.max(0, Math.min(100, s))}%` }} />
          </span>
          <span className="tabular-nums w-10 text-right">{s.toFixed(1)}</span>
        </div>
      );
    },
  }),
  col.accessor((r) => r.counts?.blocker ?? undefined, {
    id: "threads",
    header: "Threads",
    sortFn: "basic",
    sortUndefined: "last",
    sortDescFirst: true,
    cell: ({ row }) => <PriorityCountsInline counts={row.original.counts} />,
  }),
  col.accessor((r) => r.created_at ?? undefined, {
    id: "created",
    header: "Created",
    sortFn: "basic",
    sortUndefined: "last",
    sortDescFirst: true,
    cell: ({ row }) => (
      <time className="text-muted-foreground whitespace-nowrap" dateTime={row.original.created_at ?? undefined}>
        {fmtDate(row.original.created_at)}
      </time>
    ),
  }),
]);

const HEAD_CLASS: Record<string, string> = {
  score: "text-right",
};

export function Dashboard() {
  const { data, error } = usePoll(() => api.listReviews(50, 0), active, 3000);
  const [view, setView] = useState<View>("all");
  const [query, setQuery] = useState("");
  const [sorting, setSorting] = useState<SortingState>([{ id: "created", desc: true }]);
  const [pagination, setPagination] = useState<PaginationState>({ pageIndex: 0, pageSize: 10 });

  const rows = useMemo(() => {
    const items = data?.items ?? [];
    const v = VIEWS.find((x) => x.id === view)!;
    const q = query.trim().toLowerCase();
    return items.filter(
      (r) => v.match(r) && (!q || [r.run_id, ...(r.filenames ?? [])].some((s) => s.toLowerCase().includes(q))),
    );
  }, [data, view, query]);

  const table = useTable({
    features,
    data: rows,
    columns,
    state: { sorting, pagination },
    getRowId: (r) => r.run_id,
    onSortingChange: setSorting,
    onPaginationChange: setPagination,
    autoResetPageIndex: true,
  });

  const counts = useMemo(
    () => Object.fromEntries(VIEWS.map((v) => [v.id, (data?.items ?? []).filter(v.match).length])) as Record<View, number>,
    [data],
  );

  return (
    <div className="@container/main flex flex-1 flex-col gap-6 px-4 py-6 lg:px-6">
      <div className="flex flex-wrap items-end gap-x-4 gap-y-2">
        <div>
          <h1 className="text-2xl font-normal">Reviews</h1>
          <p className="text-muted-foreground mt-1">
            Automated QA for dubbed and AI-generated video. Open a review to see timestamped comments.
          </p>
        </div>
        {data && active(data) && (
          <span className="text-xs text-muted-foreground inline-flex items-center gap-1.5 mb-1" aria-live="polite">
            <span className="size-1.5 rounded-full bg-interactive animate-pulse" aria-hidden />
            Updating live
          </span>
        )}
      </div>

      {error && !data && <ErrorBox>Could not load reviews: {error.message}</ErrorBox>}

      <SectionCards data={data} />

      {data && data.items.length === 0 ? (
        <Card className="py-0">
          <Empty className="py-16">
            <EmptyHeader>
              <EmptyMedia variant="icon">
                <TrayIcon />
              </EmptyMedia>
              <EmptyTitle>No reviews yet</EmptyTitle>
              <EmptyDescription>
                Upload a dubbed video and its original to get a review with timestamped comments.
              </EmptyDescription>
            </EmptyHeader>
            <EmptyContent>
              <Button asChild>
                <Link href="/reviews/new">
                  <PlusIcon data-icon="inline-start" />
                  Upload a video
                </Link>
              </Button>
            </EmptyContent>
          </Empty>
        </Card>
      ) : (
        <Card className="gap-0 py-0">
          <div className="flex flex-wrap items-center gap-3 border-b p-3">
            <Tabs
              value={view}
              onValueChange={(v) => {
                setView(v as View);
                setPagination((p) => ({ ...p, pageIndex: 0 }));
              }}
            >
              <TabsList>
                {VIEWS.map((v) => (
                  <TabsTrigger key={v.id} value={v.id}>
                    {v.label}
                    <span className="ml-1 tabular-nums text-muted-foreground">{data ? counts[v.id] : ""}</span>
                  </TabsTrigger>
                ))}
              </TabsList>
            </Tabs>
            <div className="relative ml-auto w-full sm:w-64">
              <MagnifyingGlassIcon
                className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground"
                aria-hidden
              />
              <Input
                type="search"
                placeholder="Search file or run id"
                aria-label="Search reviews"
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value);
                  setPagination((p) => ({ ...p, pageIndex: 0 }));
                }}
                className="pl-8 bg-field"
              />
            </div>
          </div>

          <Table>
            <caption className="sr-only">Reviews</caption>
            <TableHeader className="bg-muted">
              {table.getHeaderGroups().map((hg) => (
                <TableRow key={hg.id} className="hover:bg-transparent">
                  {hg.headers.map((h) => {
                    const sorted = h.column.getIsSorted();
                    const Icon = sorted === "asc" ? ArrowUpIcon : sorted === "desc" ? ArrowDownIcon : ArrowsDownUpIcon;
                    return (
                      <TableHead
                        key={h.id}
                        className={cx("h-10 px-3", HEAD_CLASS[h.column.id])}
                        aria-sort={sorted === "asc" ? "ascending" : sorted === "desc" ? "descending" : undefined}
                      >
                        {h.column.getCanSort() ? (
                          <button
                            type="button"
                            onClick={h.column.getToggleSortingHandler()}
                            className={cx(
                              "inline-flex items-center gap-1 font-medium hover:text-foreground",
                              HEAD_CLASS[h.column.id] && "flex-row-reverse",
                            )}
                          >
                            <FlexRender header={h} />
                            <Icon className={cx("size-3.5", !sorted && "opacity-40")} aria-hidden />
                          </button>
                        ) : (
                          <FlexRender header={h} />
                        )}
                      </TableHead>
                    );
                  })}
                </TableRow>
              ))}
            </TableHeader>
            <TableBody>
              {!data ? (
                [0, 1, 2, 3].map((i) => (
                  <TableRow key={i}>
                    <TableCell colSpan={columns.length} className="px-3 py-3">
                      <Skeleton className="h-8" />
                    </TableCell>
                  </TableRow>
                ))
              ) : table.getRowModel().rows.length ? (
                table.getRowModel().rows.map((row) => (
                  <TableRow key={row.id} className="relative align-top">
                    {row.getAllCells().map((cell) => (
                      <TableCell
                        key={cell.id}
                        className={cx("px-3 py-2.5 whitespace-normal", cell.column.id === "score" && "text-right")}
                      >
                        <FlexRender cell={cell} />
                      </TableCell>
                    ))}
                  </TableRow>
                ))
              ) : (
                <TableRow>
                  <TableCell colSpan={columns.length} className="h-24 text-center text-muted-foreground">
                    No reviews match.
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>

          <div className="flex items-center gap-3 border-t px-3 h-12 text-sm text-muted-foreground">
            <span>
              {rows.length} of {data?.items.length ?? 0} shown
              {data && data.total > data.items.length && ` · latest ${data.items.length} of ${data.total}`}
            </span>
            <span className="ml-auto tabular-nums">
              Page {pagination.pageIndex + 1} of {Math.max(table.getPageCount(), 1)}
            </span>
            <div className="flex gap-1">
              <Button
                variant="outline"
                size="icon-sm"
                onClick={() => table.previousPage()}
                disabled={!table.getCanPreviousPage()}
                aria-label="Previous page"
              >
                <CaretLeftIcon />
              </Button>
              <Button
                variant="outline"
                size="icon-sm"
                onClick={() => table.nextPage()}
                disabled={!table.getCanNextPage()}
                aria-label="Next page"
              >
                <CaretRightIcon />
              </Button>
            </div>
          </div>
        </Card>
      )}
    </div>
  );
}
