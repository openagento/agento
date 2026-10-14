import { useEffect } from "react";
import type { ComponentType } from "react";
import { AppShell, Burger, Group, NavLink, Text, useComputedColorScheme, useMantineColorScheme } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { NavLink as RouterLink, Outlet, useLocation, useNavigate } from "react-router";
import { logout, useCan, useSession, type PanelModule } from "@agento/api";
import {
  Blocks, Bot, House, KeyRound, LayoutDashboard, ListChecks, LogOut, Moon, Settings, ShieldCheck, Sparkles, Sun,
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

/** Each screen and the admin leaves that show it (any one is enough): Users holds the Roles tab too. */
const ADMIN: [string, string, Parameters<typeof icon>[0], string[]][] = [
  ["/", "Dashboard", LayoutDashboard, ["admin.dashboard"]], ["/admin/jobs", "Jobs", ListChecks, ["admin.jobs"]],
  ["/admin/agents", "Agents", Bot, ["admin.agents"]], ["/admin/credentials", "Credentials", KeyRound, ["admin.credentials"]],
  ["/admin/tools", "Tools", Wrench, ["admin.tools"]], ["/admin/skills", "Skills", Sparkles, ["admin.skills"]],
  ["/admin/config", "Config", Settings, ["admin.config"]], ["/users", "Users", Users, ["admin.users", "admin.roles"]],
];

/** The NavbarLinksGroup pattern (frontend/lookbook/NavbarLinksGroup) as a nested Mantine NavLink.
 *  A convenience only: every /api/admin route refuses a session without its leaf (403). */
export function AdminNav({ onNavigate }: { onNavigate: () => void }) {
  const acl = useSession()?.acl ?? [];
  const { pathname } = useLocation();
  const items = ADMIN.filter(([, , , leaves]) => leaves.some((l) => acl.includes(l)));
  if (!items.length) return null;
  return (
    <NavLink label="Administration" fw={500} leftSection={icon(ShieldCheck)} defaultOpened childrenOffset="md">
      {items.map(([to, label, Icon]) => navItem(to, label, Icon, pathname, onNavigate))}
    </NavLink>
  );
}

export function Shell({ modules }: { modules: PanelModule[] }) {
  const [opened, { toggle, close }] = useDisclosure();
  const user = useSession();
  const dashboard = useCan("admin.dashboard");
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
          {/* With the Dashboard leaf, Home is the Dashboard, in the Administration group. */}
          {!dashboard && navItem("/", "Home", House, location.pathname, close)}
          {modules.map((m) => <ModuleNav key={m.id} module={m} onNavigate={close} />)}
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
