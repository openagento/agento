import { useEffect } from "react";
import type { ComponentType } from "react";
import { AppShell, Burger, Group, NavLink, Text, useComputedColorScheme, useMantineColorScheme } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { NavLink as RouterLink, Outlet, useLocation, useNavigate } from "react-router";
import { logout, useSession, type PanelModule } from "@agento/api";
import {
  AppWindow, Blocks, Bot, House, KeyRound, LayoutDashboard, ListChecks, LogOut, Moon, Settings, ShieldCheck, Sparkles, Sun,
  Users, Wrench,
} from "lucide-react";
import { useAvailable } from "./availability";

// The NavbarSimple pattern from ui.mantine.dev (frontend/lookbook/NavbarSimple): icon links, and
// the session actions in a footer under the links.
const icon = (Icon: ComponentType<{ size?: number; strokeWidth?: number }>) => <Icon size={20} strokeWidth={1.5} aria-hidden />;

export function ModuleNav({ module, onNavigate }: { module: PanelModule; onNavigate: () => void }) {
  const location = useLocation();
  if (useAvailable(module) !== "on") return null;
  return (
    <>
      {module.routes.filter((r) => r.nav).map((r) => (
        <NavLink key={r.path} component={RouterLink} to={r.path} label={r.nav} onClick={onNavigate} fw={500}
          // ponytail: one icon for every module screen; add `icon` to the route contract when a module needs its own.
          leftSection={icon(Blocks)}
          active={location.pathname === r.path || location.pathname.startsWith(r.path + "/")} />
      ))}
    </>
  );
}

/** One nav link; "/" is active on "/" only, any other path on itself and below. */
const navItem = (to: string, label: string, Icon: Parameters<typeof icon>[0], pathname: string, onNavigate: () => void) => (
  <NavLink key={to} component={RouterLink} to={to} label={label} onClick={onNavigate} fw={500} leftSection={icon(Icon)}
    active={to === "/" ? pathname === "/" : pathname === to || pathname.startsWith(to + "/")} />
);

const ADMIN: [string, string, Parameters<typeof icon>[0]][] = [
  ["/", "Dashboard", LayoutDashboard], ["/admin/jobs", "Jobs", ListChecks], ["/admin/agents", "Agents", Bot],
  ["/admin/credentials", "Credentials", KeyRound], ["/admin/tools", "Tools", Wrench], ["/admin/skills", "Skills", Sparkles],
  ["/admin/config", "Config", Settings], ["/users", "Users", Users],
];

/** The NavbarLinksGroup pattern (frontend/lookbook/NavbarLinksGroup) as a nested Mantine NavLink.
 *  A convenience only: every /api/admin route refuses a user-role session (403). */
export function AdminNav({ onNavigate }: { onNavigate: () => void }) {
  const user = useSession();
  const { pathname } = useLocation();
  if (user?.role !== "admin") return null;
  return (
    <NavLink label="Administration" fw={500} leftSection={icon(ShieldCheck)} defaultOpened childrenOffset="md">
      {ADMIN.map(([to, label, Icon]) => navItem(to, label, Icon, pathname, onNavigate))}
    </NavLink>
  );
}

export function Shell({ modules }: { modules: PanelModule[] }) {
  const [opened, { toggle, close }] = useDisclosure();
  const user = useSession();
  const navigate = useNavigate();
  const location = useLocation();
  const { setColorScheme } = useMantineColorScheme();
  const scheme = useComputedColorScheme("light");
  // agento-ui.css reads data-theme; Mantine reads its own attribute. Keep them equal.
  useEffect(() => { document.documentElement.dataset.theme = scheme; }, [scheme]);

  return (
    <AppShell header={{ height: 56 }} navbar={{ width: 220, breakpoint: "sm", collapsed: { mobile: !opened } }}
      padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Group>
            <Burger opened={opened} onClick={toggle} hiddenFrom="sm" size="sm" aria-label="Toggle navigation" />
            <strong>Agento</strong>
          </Group>
          <Text size="sm" c="dimmed">{user?.username}</Text>
        </Group>
      </AppShell.Header>
      <AppShell.Navbar p="md" aria-label="Main">
        <AppShell.Section grow>
          {/* An admin's Home is the Dashboard, in the Administration group. */}
          {user?.role !== "admin" && navItem("/", "Home", House, location.pathname, close)}
          {modules.map((m) => <ModuleNav key={m.id} module={m} onNavigate={close} />)}
          {navItem("/miniapps", "Miniapps", AppWindow, location.pathname, close)}
          <AdminNav onNavigate={close} />
        </AppShell.Section>
        <AppShell.Section pt="md" style={{ borderTop: "1px solid var(--mantine-color-default-border)" }}>
          <NavLink component="button" fw={500} leftSection={icon(scheme === "dark" ? Sun : Moon)}
            label={scheme === "dark" ? "Light theme" : "Dark theme"}
            onClick={() => setColorScheme(scheme === "dark" ? "light" : "dark")} />
          <NavLink component="button" fw={500} leftSection={icon(LogOut)} label="Sign out"
            onClick={async () => { await logout().catch(() => undefined); navigate("/login"); }} />
        </AppShell.Section>
      </AppShell.Navbar>
      <AppShell.Main><Outlet /></AppShell.Main>
    </AppShell>
  );
}
