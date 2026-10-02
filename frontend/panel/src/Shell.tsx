import { useEffect } from "react";
import type { ComponentType } from "react";
import { AppShell, Burger, Group, NavLink, Text, useComputedColorScheme, useMantineColorScheme } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { NavLink as RouterLink, Outlet, useLocation, useNavigate } from "react-router";
import { logout, useSession, type PanelModule } from "@agento/api";
import { AppWindow, Blocks, House, LogOut, Moon, Sun, Users } from "lucide-react";
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

export function Shell({ modules }: { modules: PanelModule[] }) {
  const [opened, { toggle, close }] = useDisclosure();
  const user = useSession();
  const navigate = useNavigate();
  const location = useLocation();
  const { setColorScheme } = useMantineColorScheme();
  const scheme = useComputedColorScheme("light");
  // agento-ui.css reads data-theme; Mantine reads its own attribute. Keep them equal.
  useEffect(() => { document.documentElement.dataset.theme = scheme; }, [scheme]);

  const item = (to: string, label: string, Icon: Parameters<typeof icon>[0]) => (
    <NavLink component={RouterLink} to={to} label={label} onClick={close} fw={500} leftSection={icon(Icon)}
      active={to === "/" ? location.pathname === "/" : location.pathname.startsWith(to)} />
  );
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
          {item("/", "Home", House)}
          {modules.map((m) => <ModuleNav key={m.id} module={m} onNavigate={close} />)}
          {item("/miniapps", "Miniapps", AppWindow)}
          {/* A convenience only: the API refuses a user-role session (403). */}
          {user?.role === "admin" && item("/users", "Users", Users)}
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
