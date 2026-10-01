import { lazy } from "react";
import { createBrowserRouter, redirect, type RouteObject } from "react-router";
import { boot, getUser, onExpired, type PanelModule } from "@agento/api";
import { MODULES } from "./generated/registry";
import { Shell } from "./Shell";
import { ModuleRoute } from "./ModuleRoute";
import { safeNext } from "./safeNext";

let booted: Promise<unknown> | null = null;

/** Every page but /login needs a session; the first visit asks `GET /api/session`. */
async function requireSession({ request }: { request: Request }) {
  booted ??= boot().catch(() => null);
  await booted;
  if (getUser()) return null;
  const url = new URL(request.url);
  throw redirect(`/login?next=${encodeURIComponent(url.pathname + url.search)}`);
}

export function moduleRoutes(modules: PanelModule[]): RouteObject[] {
  return modules.flatMap((m) => m.routes.map((r) => ({
    path: r.path.replace(/^\//, ""),
    element: <ModuleRoute module={m} Screen={lazy(r.load)} />,
  })));
}

export const routes: RouteObject[] = [
  { path: "/login", lazy: async () => ({ Component: (await import("./routes/Login")).Login }) },
  {
    path: "/",
    loader: requireSession,
    element: <Shell modules={MODULES} />,
    children: [
      { index: true, lazy: async () => ({ Component: (await import("./routes/Home")).Home }) },
      { path: "users", lazy: async () => ({ Component: (await import("./routes/Users")).Users }) },
      { path: "miniapps", lazy: async () => ({ Component: (await import("./routes/Miniapps")).Miniapps }) },
      ...moduleRoutes(MODULES),
      { path: "*", lazy: async () => ({ Component: (await import("./routes/NotFound")).NotFound }) },
    ],
  },
];

export const router = createBrowserRouter(routes);

// The first 401 ends the session (endSession) and lands here; `next` is a path only.
onExpired(() => {
  booted = Promise.resolve();
  const here = window.location.pathname + window.location.search;
  void router.navigate(`/login?next=${encodeURIComponent(safeNext(here))}`);
});
