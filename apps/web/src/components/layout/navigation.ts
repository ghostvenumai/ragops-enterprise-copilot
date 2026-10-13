import { Activity, Bot, Coins, FileSearch, LayoutDashboard, Library, ShieldCheck, type LucideIcon } from "lucide-react";
import { isAdmin } from "@/lib/roles";

export type NavKey = "cockpit" | "copilot" | "knowledge" | "intelligence" | "monitoring" | "finops" | "governance";

export interface NavItem {
  key: NavKey;
  href: string;
  icon: LucideIcon;
  /** Visibility hint only; the API enforces access on every request. */
  visible: (roles: readonly string[]) => boolean;
}

const everyone = () => true;

export const NAV_ITEMS: readonly NavItem[] = [
  { key: "cockpit", href: "/", icon: LayoutDashboard, visible: everyone },
  { key: "copilot", href: "/copilot", icon: Bot, visible: everyone },
  { key: "knowledge", href: "/knowledge", icon: Library, visible: everyone },
  { key: "intelligence", href: "/intelligence", icon: FileSearch, visible: everyone },
  { key: "monitoring", href: "/monitoring", icon: Activity, visible: isAdmin },
  { key: "finops", href: "/finops", icon: Coins, visible: isAdmin },
  { key: "governance", href: "/governance", icon: ShieldCheck, visible: isAdmin },
];

export function isActive(href: string, pathname: string): boolean {
  return href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);
}
