import { useEffect, useState } from "react";
import type { ComponentType } from "react";
import { AppShell, Burger, Group, NavLink, useComputedColorScheme, useMantineColorScheme } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { notifications } from "@mantine/notifications";
import { NavLink as RouterLink, Outlet, useLocation, useNavigate } from "react-router";
import { getUser, logout, useCan, useSession, type PanelModule } from "@agento/api";
import { Button, PRODUCT_NAME, useLookup, useT, type Translate } from "@agento/ui";
import {
  Blocks, Bot, CircleUser, House, KeyRound, LayoutDashboard, ListChecks, LogOut, Moon, Settings, ShieldCheck, Sparkles, Sun,
  Users, Wrench,
} from "lucide-react";
import { useAvailable } from "./availability";
import { ProfileDialog } from "./ProfileDialog";

// The NavbarSimple pattern from ui.mantine.dev (frontend/lookbook/NavbarSimple): icon links, and
// the session actions in a footer under the links.
const icon = (Icon: ComponentType<{ size?: number; strokeWidth?: number }>) => <Icon size={20} strokeWidth={1.5} aria-hidden />;

export function ModuleNav({ module, onNavigate }: { module: PanelModule; onNavigate: () => void }) {
  const location = useLocation();
  const lookup = useLookup();
  if (useAvailable(module) !== "on") return null;
  return (
    <>
      {module.routes.filter((r) => r.nav).map((r) => (
        <NavLink key={r.path} component={RouterLink} to={r.path} label={lookup(module.id, `nav.${r.path}`) ?? r.nav} onClick={onNavigate} fw={500}
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
const adminScreens = (t: Translate): [string, string, Parameters<typeof icon>[0], string[]][] => [
  ["/", t("nav.dashboard"), LayoutDashboard, ["admin.dashboard"]], ["/admin/jobs", t("nav.jobs"), ListChecks, ["admin.jobs"]],
  ["/admin/agents", t("nav.agents"), Bot, ["admin.agents"]],
  ["/admin/credentials", t("nav.credentials"), KeyRound, ["admin.credentials"]],
  ["/admin/tools", t("nav.tools"), Wrench, ["admin.tools"]], ["/admin/skills", t("nav.skills"), Sparkles, ["admin.skills"]],
  ["/admin/config", t("nav.config"), Settings, ["admin.config"]], ["/users", t("nav.users"), Users, ["admin.users", "admin.roles"]],
];

/** The NavbarLinksGroup pattern (frontend/lookbook/NavbarLinksGroup) as a nested Mantine NavLink.
 *  A convenience only: every /api/admin route refuses a session without its leaf (403). */
export function AdminNav({ onNavigate }: { onNavigate: () => void }) {
  const acl = useSession()?.acl ?? [];
  const { pathname } = useLocation();
  const t = useT("panel");
  const items = adminScreens(t).filter(([, , , leaves]) => leaves.some((l) => acl.includes(l)));
  if (!items.length) return null;
  return (
    <NavLink label={t("nav.administration")} fw={500} leftSection={icon(ShieldCheck)} defaultOpened childrenOffset="md">
      {items.map(([to, label, Icon]) => navItem(to, label, Icon, pathname, onNavigate))}
    </NavLink>
  );
}

export function Shell({ modules }: { modules: PanelModule[] }) {
  const [opened, { toggle, close }] = useDisclosure();
  const [profileOpened, profile] = useDisclosure();
  const user = useSession();
  const dashboard = useCan("admin.dashboard");
  const navigate = useNavigate();
  const location = useLocation();
  const { setColorScheme } = useMantineColorScheme();
  const scheme = useComputedColorScheme("light");
  const t = useT("panel");
  const [leaving, setLeaving] = useState(false);
  // A sign-out that fails keeps the session and says so. When the session ended on another path
  // during the request (the first 401 of another request), that path owns the navigation: its
  // address keeps `next`, and the sign-in page shows no sign-out failure.
  const signOut = async () => {
    if (leaving) return;
    setLeaving(true);
    try {
      await logout();
    } catch {
      if (!getUser()) return;
      setLeaving(false);
      notifications.show({ color: "red", message: t("shell.sign_out_failed") });
      return;
    }
    notifications.clean();
    navigate("/login");
  };
  // agento-ui.css reads data-theme; Mantine reads its own attribute. Keep them equal.
  useEffect(() => { document.documentElement.dataset.theme = scheme; }, [scheme]);
  if (!user) return null;

  return (
    <AppShell header={{ height: 56 }} navbar={{ width: 220, breakpoint: "sm", collapsed: { mobile: !opened } }}
      padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Group>
            <Burger opened={opened} onClick={toggle} hiddenFrom="sm" size="sm" aria-label={t("shell.toggle_navigation")} />
            <strong>{PRODUCT_NAME}</strong>
          </Group>
          <Button variant="subtle" onClick={profile.open}
            aria-label={t("shell.profile_of", { username: user.username })} aria-haspopup="dialog">
            <Group gap={6} wrap="nowrap"><CircleUser size={18} strokeWidth={1.5} aria-hidden />{user.username}</Group>
          </Button>
        </Group>
      </AppShell.Header>
      <AppShell.Navbar p="md" aria-label={t("shell.main_navigation")}>
        <AppShell.Section grow>
          {/* With the Dashboard leaf, Home is the Dashboard, in the Administration group. */}
          {!dashboard && navItem("/", t("nav.home"), House, location.pathname, close)}
          {modules.map((m) => <ModuleNav key={m.id} module={m} onNavigate={close} />)}
          <AdminNav onNavigate={close} />
        </AppShell.Section>
        <AppShell.Section pt="md" style={{ borderTop: "1px solid var(--mantine-color-default-border)" }}>
          <NavLink component="button" fw={500} leftSection={icon(scheme === "dark" ? Sun : Moon)}
            label={scheme === "dark" ? t("shell.light_theme") : t("shell.dark_theme")}
            onClick={() => setColorScheme(scheme === "dark" ? "light" : "dark")} />
          <NavLink component="button" fw={500} leftSection={icon(LogOut)} label={t("shell.sign_out")}
            disabled={leaving} onClick={signOut} />
        </AppShell.Section>
      </AppShell.Navbar>
      <AppShell.Main><Outlet /></AppShell.Main>
      <ProfileDialog opened={profileOpened} onClose={profile.close} />
    </AppShell>
  );
}
