"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Fragment } from "react";
import { PlusIcon } from "@phosphor-icons/react";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { SidebarTrigger } from "@/components/ui/sidebar";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { IS_MOCK } from "@/lib/api";
import { ThemeToggle } from "./theme-toggle";

function crumbs(pathname: string): { label: string; href?: string; mono?: boolean }[] {
  if (pathname === "/reviews/new") return [{ label: "Reviews", href: "/" }, { label: "New review" }];
  const m = pathname.match(/^\/reviews\/([^/]+)/);
  if (m) return [{ label: "Reviews", href: "/" }, { label: decodeURIComponent(m[1]), mono: true }];
  return [{ label: "Reviews" }];
}

export function SiteHeader() {
  const pathname = usePathname();
  const items = crumbs(pathname);
  return (
    <header className="sticky top-0 z-20 flex h-(--header-height) shrink-0 items-center gap-2 border-b bg-card">
      <div className="flex w-full items-center gap-1 px-4 lg:gap-2 lg:px-6">
        <SidebarTrigger className="-ml-1" />
        <Separator orientation="vertical" className="mx-2 data-[orientation=vertical]:h-4" />
        <Breadcrumb className="min-w-0">
          <BreadcrumbList className="flex-nowrap">
            {items.map((c, i) => (
              <Fragment key={i}>
                {i > 0 && <BreadcrumbSeparator />}
                <BreadcrumbItem className="min-w-0">
                  {c.href ? (
                    <BreadcrumbLink asChild>
                      <Link href={c.href}>{c.label}</Link>
                    </BreadcrumbLink>
                  ) : (
                    <BreadcrumbPage className={c.mono ? "truncate font-mono text-xs" : "font-medium"}>{c.label}</BreadcrumbPage>
                  )}
                </BreadcrumbItem>
              </Fragment>
            ))}
          </BreadcrumbList>
        </Breadcrumb>
        <div className="ml-auto flex items-center gap-2">
          {IS_MOCK && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Badge variant="outline" className="border-shouldfix/40 bg-shouldfix-soft text-shouldfix">
                  Mock API
                </Badge>
              </TooltipTrigger>
              <TooltipContent className="max-w-64">
                NEXT_PUBLIC_VREVIEW_MOCK=1: review data comes from a local fixture, not the vreview API. Sign-in is real.
              </TooltipContent>
            </Tooltip>
          )}
          {pathname !== "/reviews/new" && (
            <Button asChild size="sm" className="hidden sm:inline-flex">
              <Link href="/reviews/new">
                <PlusIcon data-icon="inline-start" />
                New review
              </Link>
            </Button>
          )}
          <ThemeToggle />
        </div>
      </div>
    </header>
  );
}
