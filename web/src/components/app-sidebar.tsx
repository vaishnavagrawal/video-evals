"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { FilmSlateIcon } from "@phosphor-icons/react";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
} from "@/components/ui/sidebar";
import { NavMain } from "./nav-main";
import { NavUser } from "./nav-user";

export function AppSidebar({
  user,
  ...props
}: React.ComponentProps<typeof Sidebar> & { user: { name: string; email: string; image?: string | null } }) {
  const pathname = usePathname();
  return (
    <Sidebar collapsible="icon" {...props}>
      <SidebarHeader className="border-b border-sidebar-border">
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton size="lg" asChild tooltip="vreview">
              <Link href="/">
                <span className="flex aspect-square size-8 items-center justify-center bg-primary text-primary-foreground">
                  <FilmSlateIcon className="size-4" weight="bold" />
                </span>
                <span className="grid flex-1 text-left leading-tight">
                  <span className="truncate font-semibold text-sidebar-accent-foreground">vreview</span>
                  <span className="truncate text-xs">Dub QA</span>
                </span>
              </Link>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>
      <SidebarContent>
        <NavMain pathname={pathname} />
      </SidebarContent>
      <SidebarFooter className="border-t border-sidebar-border">
        <NavUser user={user} />
      </SidebarFooter>
      <SidebarRail />
    </Sidebar>
  );
}
