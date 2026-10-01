import { useEffect } from "react";
import { AppShell, Burger, Group, NavLink, useComputedColorScheme, useMantineColorScheme } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { NavLink as RouterLink, Outlet, useLocation, useNavigate } from "react-router";
import { logout, useSession, type PanelModule } from "@agento/api";
import { Button } from "@agento/ui";
import { useAvailable } from "./availability";

export function ModuleNav({ module, onNavigate }: { module: PanelModule; onNavigate: () => void }) {
  const location = useLocation();
  if (useAvailable(module) !== "on") return null;
  return (
    <>
      {module.routes.filter((r) => r.nav).map((r) => (
        <NavLink key={r.path} component={RouterLink} to={r.path} label={r.nav} onClick={onNavigate}
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

  const item = (to: string, label: string) => (
    <NavLink component={RouterLink} to={to} label={label} onClick={close}
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
          <Group gap="xs">
            <Button variant="subtle" onClick={() => setColorScheme(scheme === "dark" ? "light" : "dark")}>
              {scheme === "dark" ? "Light theme" : "Dark theme"}
            </Button>
            <span className="ag-muted">{user?.username}</span>
            <Button onClick={async () => { await logout().catch(() => undefined); navigate("/login"); }}>Sign out</Button>
          </Group>
        </Group>
      </AppShell.Header>
      <AppShell.Navbar p="xs" aria-label="Main">
        {item("/", "Home")}
        {modules.map((m) => <ModuleNav key={m.id} module={m} onNavigate={close} />)}
        {item("/miniapps", "Miniapps")}
        {/* A convenience only: the API refuses a user-role session (403). */}
        {user?.role === "admin" && item("/users", "Users")}
      </AppShell.Navbar>
      <AppShell.Main><Outlet /></AppShell.Main>
    </AppShell>
  );
}
