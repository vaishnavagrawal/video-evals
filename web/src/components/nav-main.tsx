"use client";
import Link from "next/link";
import { ListChecksIcon, PlusIcon } from "@phosphor-icons/react";
import {
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar";

const items = [
  { title: "Reviews", url: "/", icon: ListChecksIcon, match: (p: string) => p === "/" || /^\/reviews\/(?!new$)/.test(p) },
  { title: "New review", url: "/reviews/new", icon: PlusIcon, match: (p: string) => p === "/reviews/new" },
];

export function NavMain({ pathname }: { pathname: string }) {
  return (
    <SidebarGroup>
      <SidebarGroupLabel>Workspace</SidebarGroupLabel>
      <SidebarGroupContent>
        <SidebarMenu>
          {items.map((item) => {
            const active = item.match(pathname);
            return (
              <SidebarMenuItem key={item.url}>
                {/* Carbon UI shell: the selected item gets a 3px interactive bar on its leading edge. */}
                <SidebarMenuButton
                  asChild
                  isActive={active}
                  tooltip={item.title}
                  className="relative data-[active=true]:before:absolute data-[active=true]:before:inset-y-0 data-[active=true]:before:left-0 data-[active=true]:before:w-[3px] data-[active=true]:before:bg-interactive"
                >
                  <Link href={item.url} aria-current={active ? "page" : undefined}>
                    <item.icon />
                    <span>{item.title}</span>
                  </Link>
                </SidebarMenuButton>
              </SidebarMenuItem>
            );
          })}
        </SidebarMenu>
      </SidebarGroupContent>
    </SidebarGroup>
  );
}
