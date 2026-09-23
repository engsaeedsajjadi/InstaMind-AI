"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { getWorkspaceId, isAuthenticated, logout } from "@/lib/api";
import { Spinner } from "./ui";

const NAV = [
  { href: "/dashboard", label: "داشبورد" },
  { href: "/instagram", label: "اتصال اینستاگرام" },
];

function ThemeToggle() {
  const [dark, setDark] = useState(false);
  useEffect(() => {
    const stored = window.localStorage.getItem("instamind.theme");
    const initial = stored ? stored === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
    setDark(initial);
    document.documentElement.classList.toggle("dark", initial);
  }, []);
  return (
    <button
      type="button"
      aria-label={dark ? "حالت روشن" : "حالت تیره"}
      className="rounded-xl p-2 text-zinc-500 transition hover:bg-zinc-100 dark:text-zinc-400 dark:hover:bg-zinc-800"
      onClick={() => {
        const next = !dark;
        setDark(next);
        document.documentElement.classList.toggle("dark", next);
        window.localStorage.setItem("instamind.theme", next ? "dark" : "light");
      }}
    >
      {dark ? "☀️" : "🌙"}
    </button>
  );
}

/**
 * Client-side guard + app chrome. Redirects to /login when there is no
 * session; renders children only after the check to avoid content flash.
 */
export function AppShell({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (!isAuthenticated()) {
      router.replace("/login");
      return;
    }
    setReady(true);
  }, [router]);

  if (!ready) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner className="h-7 w-7 text-brand-600" />
      </div>
    );
  }

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-40 border-b border-zinc-200 bg-white/80 backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/80">
        <div className="mx-auto flex h-14 max-w-5xl items-center gap-2 px-4">
          <Link href="/dashboard" className="flex items-center gap-2 font-bold">
            <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-gradient-to-br from-fuchsia-600 via-rose-500 to-amber-400 text-sm text-white">
              IM
            </span>
            اینستامایند
          </Link>
          <nav className="ms-4 flex items-center gap-1">
            {NAV.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                className={`rounded-lg px-3 py-1.5 text-sm transition ${
                  pathname.startsWith(item.href)
                    ? "bg-brand-50 font-semibold text-brand-700 dark:bg-brand-950 dark:text-brand-300"
                    : "text-zinc-600 hover:bg-zinc-100 dark:text-zinc-300 dark:hover:bg-zinc-800"
                }`}
              >
                {item.label}
              </Link>
            ))}
          </nav>
          <div className="ms-auto flex items-center gap-1">
            {!getWorkspaceId() ? null : <WorkspaceChip />}
            <ThemeToggle />
            <button
              type="button"
              className="rounded-lg px-3 py-1.5 text-sm text-zinc-600 transition hover:bg-zinc-100 dark:text-zinc-300 dark:hover:bg-zinc-800"
              onClick={() => {
                void logout().finally(() => router.replace("/login"));
              }}
            >
              خروج
            </button>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-5xl px-4 py-8">{children}</main>
    </div>
  );
}

import { fetchWorkspaces } from "@/lib/api";
import type { WorkspaceRead } from "@/lib/types";

function WorkspaceChip() {
  const [workspace, setWorkspace] = useState<WorkspaceRead | null>(null);
  useEffect(() => {
    const id = getWorkspaceId();
    void fetchWorkspaces()
      .then((rows) => setWorkspace(rows.find((row) => row.id === id) ?? rows[0] ?? null))
      .catch(() => setWorkspace(null));
  }, []);
  if (!workspace) return null;
  return (
    <span className="hidden rounded-full bg-zinc-100 px-3 py-1 text-xs text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300 sm:inline">
      {workspace.name}
    </span>
  );
}
