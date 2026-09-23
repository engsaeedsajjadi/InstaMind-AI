"use client";

import Link from "next/link";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { AppShell } from "@/components/shell";
import { useToast } from "@/components/toaster";
import { Badge, Button, Card, EmptyState, Field, Skeleton } from "@/components/ui";
import {
  createWorkspace,
  fetchAccounts,
  fetchContents,
  fetchPublishingJobs,
  fetchWorkspaces,
  getWorkspaceId,
  setWorkspaceId,
} from "@/lib/api";
import type { WorkspaceRead } from "@/lib/types";

interface Stats {
  accounts: number;
  contents: number;
  jobs: number;
}

export default function DashboardPage() {
  const toast = useToast();
  const [loading, setLoading] = useState(true);
  const [workspace, setWorkspace] = useState<WorkspaceRead | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const workspaces = await fetchWorkspaces();
      const activeId = getWorkspaceId();
      const active = workspaces.find((row) => row.id === activeId) ?? workspaces[0] ?? null;
      setWorkspace(active);
      if (!active) {
        setStats(null);
        return;
      }
      setWorkspaceId(active.id);
      const [accounts, contents, jobs] = await Promise.all([
        fetchAccounts(),
        fetchContents(),
        fetchPublishingJobs(),
      ]);
      setStats({ accounts: accounts.length, contents: contents.length, jobs: jobs.length });
    } catch {
      toast.push("دریافت اطلاعات داشبورد ناموفق بود.", "error");
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => {
    void load();
  }, [load]);

  async function onCreateWorkspace(event: FormEvent) {
    event.preventDefault();
    setCreating(true);
    try {
      const created = await createWorkspace(name);
      setWorkspaceId(created.id);
      toast.push("ورک‌اسپیس ساخته شد.", "success");
      await load();
    } catch {
      toast.push("ساخت ورک‌اسپیس ناموفق بود.", "error");
    } finally {
      setCreating(false);
    }
  }

  return (
    <AppShell>
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-xl font-bold">داشبورد</h1>
        {workspace ? <Badge tone="blue">{workspace.name}</Badge> : null}
      </div>

      {loading ? (
        <div className="grid gap-4 sm:grid-cols-3">
          <Skeleton className="h-28" />
          <Skeleton className="h-28" />
          <Skeleton className="h-28" />
        </div>
      ) : !workspace ? (
        <Card className="mx-auto max-w-md">
          <h2 className="font-semibold">اولین ورک‌اسپیس خود را بسازید</h2>
          <p className="mt-1 text-sm leading-6 text-zinc-500">
            هر برند یا کسب‌وکار یک ورک‌اسپیس است. اکانت‌های اینستاگرام و محتوا به آن متصل می‌شوند.
          </p>
          <form onSubmit={onCreateWorkspace} className="mt-4 flex flex-col gap-3">
            <Field
              label="نام ورک‌اسپیس"
              required
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="مثلاً کافه‌ی من"
            />
            <Button type="submit" busy={creating}>
              ساخت ورک‌اسپیس
            </Button>
          </form>
        </Card>
      ) : (
        <div className="flex flex-col gap-6">
          <div className="grid gap-4 sm:grid-cols-3">
            <Card>
              <p className="text-sm text-zinc-500">اکانت‌های متصل</p>
              <p className="mt-2 text-3xl font-bold">{stats?.accounts ?? 0}</p>
            </Card>
            <Card>
              <p className="text-sm text-zinc-500">محتواها</p>
              <p className="mt-2 text-3xl font-bold">{stats?.contents ?? 0}</p>
            </Card>
            <Card>
              <p className="text-sm text-zinc-500">کارهای انتشار</p>
              <p className="mt-2 text-3xl font-bold">{stats?.jobs ?? 0}</p>
            </Card>
          </div>

          {stats && stats.accounts === 0 ? (
            <Card className="border-brand-200 bg-brand-50 dark:border-brand-900 dark:bg-brand-950/40">
              <div className="flex flex-wrap items-center justify-between gap-4">
                <div>
                  <h2 className="font-semibold">ایستگاه بعدی: اتصال اینستاگرام</h2>
                  <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
                    برای شروع، اکانت Business یا Creator خود را از طریق ورود رسمی متا متصل کنید.
                  </p>
                </div>
                <Link
                  href="/instagram"
                  className="rounded-xl bg-brand-600 px-5 py-2.5 text-sm font-semibold text-white hover:bg-brand-700"
                >
                  اتصال اینستاگرام ←
                </Link>
              </div>
            </Card>
          ) : null}

          <Card>
            <h2 className="mb-4 font-semibold">چک‌لیست راه‌اندازی</h2>
            <ol className="flex flex-col gap-3 text-sm">
              <ChecklistItem done>ساخت ورک‌اسپیس</ChecklistItem>
              <ChecklistItem done={(stats?.accounts ?? 0) > 0}>اتصال اکانت اینستاگرام</ChecklistItem>
              <ChecklistItem done={(stats?.contents ?? 0) > 0}>ساخت اولین محتوا</ChecklistItem>
              <ChecklistItem done={(stats?.jobs ?? 0) > 0}>زمان‌بندی اولین انتشار</ChecklistItem>
            </ol>
          </Card>

          {stats && stats.accounts === 0 && stats.contents === 0 ? (
            <EmptyState
              title="هنوز داده‌ای نیست — و این خوب است"
              description="این داشبورد فقط داده‌ی واقعی نشان می‌دهد. با اتصال اکانت و ساخت محتوا، همین‌جا زنده می‌شود."
            />
          ) : null}
        </div>
      )}
    </AppShell>
  );
}

function ChecklistItem({ done, children }: { done: boolean; children: React.ReactNode }) {
  return (
    <li className="flex items-center gap-3">
      <span
        className={`flex h-6 w-6 items-center justify-center rounded-full text-xs font-bold ${
          done
            ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/60 dark:text-emerald-300"
            : "bg-zinc-100 text-zinc-400 dark:bg-zinc-800 dark:text-zinc-500"
        }`}
        aria-hidden="true"
      >
        {done ? "✓" : "•"}
      </span>
      <span className={done ? "" : "text-zinc-500"}>{children}</span>
    </li>
  );
}
