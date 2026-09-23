"use client";

import { useCallback, useEffect, useState } from "react";

import { AppShell } from "@/components/shell";
import { useToast } from "@/components/toaster";
import { Badge, Button, Card, EmptyState, InstagramIcon, Skeleton } from "@/components/ui";
import {
  ApiError,
  disconnectAccount,
  fetchAccounts,
  refreshAccountToken,
  startConnect,
} from "@/lib/api";
import type { SocialAccountRead } from "@/lib/types";

const CONNECT_PATHS = [
  {
    apiPath: "INSTAGRAM_LOGIN" as const,
    title: "ورود با اینستاگرام",
    description:
      "برای اکانت‌های Business یا Creator. انتشار، کامنت، پیام و آمار — بدون نیاز به صفحه‌ی فیس‌بوک.",
  },
  {
    apiPath: "FACEBOOK_LOGIN" as const,
    title: "ورود با فیس‌بوک",
    description:
      "برای اکانت‌هایی که به یک Facebook Page متصل‌اند. همه‌ی قابلیت‌ها + Business Discovery و هشتگ‌سرچ.",
  },
];

const STATUS_TONE: Record<string, "green" | "amber" | "rose"> = {
  CONNECTED: "green",
  TOKEN_EXPIRED: "amber",
  ERROR: "rose",
  REVOKED: "rose",
  DISCONNECTED: "rose",
};

const STATUS_LABEL: Record<string, string> = {
  CONNECTED: "متصل",
  TOKEN_EXPIRED: "انقضای توکن",
  ERROR: "خطا",
  REVOKED: "لغوشده",
  DISCONNECTED: "قطع‌شده",
};

export default function InstagramPage() {
  const toast = useToast();
  const [loading, setLoading] = useState(true);
  const [accounts, setAccounts] = useState<SocialAccountRead[]>([]);
  const [connecting, setConnecting] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setAccounts(await fetchAccounts());
    } catch (error) {
      toast.push(error instanceof ApiError ? error.message : "دریافت اکانت‌ها ناموفق بود.", "error");
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => {
    void load();
  }, [load]);

  async function onConnect(apiPath: "INSTAGRAM_LOGIN" | "FACEBOOK_LOGIN") {
    setConnecting(apiPath);
    try {
      const { authorize_url } = await startConnect(apiPath);
      // Hand the browser to Meta's official login. The token never touches us.
      window.location.assign(authorize_url);
    } catch (error) {
      toast.push(
        error instanceof ApiError ? error.message : "شروع اتصال ناموفق بود.",
        "error",
      );
      setConnecting(null);
    }
  }

  async function onDisconnect(account: SocialAccountRead) {
    const confirmed = window.confirm(
      `اتصال @${account.username} قطع شود؟ توکن باطل و داده‌های کش پلتفرم حذف می‌شود.`,
    );
    if (!confirmed) return;
    setBusyId(account.id);
    try {
      await disconnectAccount(account.id, true);
      toast.push(`اتصال @${account.username} قطع شد.`, "success");
      await load();
    } catch (error) {
      toast.push(error instanceof ApiError ? error.message : "قطع اتصال ناموفق بود.", "error");
    } finally {
      setBusyId(null);
    }
  }

  async function onRefreshToken(account: SocialAccountRead) {
    setBusyId(account.id);
    try {
      await refreshAccountToken(account.id);
      toast.push(`توکن @${account.username} تازه‌سازی شد.`, "success");
      await load();
    } catch (error) {
      toast.push(error instanceof ApiError ? error.message : "تازه‌سازی توکن ناموفق بود.", "error");
    } finally {
      setBusyId(null);
    }
  }

  return (
    <AppShell>
      <div className="mb-6">
        <h1 className="text-xl font-bold">اتصال اینستاگرام</h1>
        <p className="mt-1 text-sm text-zinc-500">
          اتصال فقط از طریق ورود رسمی متا انجام می‌شود؛ توکن رمزنگاری‌شده ذخیره می‌شود و رمز شما هرگز
          به این سامانه داده نمی‌شود.
        </p>
      </div>

      <div className="mb-8 grid gap-4 sm:grid-cols-2">
        {CONNECT_PATHS.map((path) => (
          <Card key={path.apiPath} className="flex flex-col">
            <div className="mb-3 flex h-11 w-11 items-center justify-center rounded-2xl bg-gradient-to-br from-fuchsia-600 via-rose-500 to-amber-400 text-white">
              <InstagramIcon />
            </div>
            <h2 className="font-semibold">{path.title}</h2>
            <p className="mt-1 flex-1 text-sm leading-6 text-zinc-500">{path.description}</p>
            <Button
              className="mt-4"
              busy={connecting === path.apiPath}
              disabled={connecting !== null}
              onClick={() => void onConnect(path.apiPath)}
            >
              {connecting === path.apiPath ? "در حال انتقال به متا…" : "اتصال با ورود رسمی متا"}
            </Button>
          </Card>
        ))}
      </div>

      <div className="mb-8 rounded-2xl border border-amber-200 bg-amber-50 p-4 text-sm leading-6 text-amber-900 dark:border-amber-800 dark:bg-amber-950/60 dark:text-amber-100">
        <strong>پیش‌نیاز:</strong> حساب اینستاگرام باید <b>Business یا Creator</b> باشد (تبدیل در
        تنظیمات اینستاگرام رایگان است). در حالت توسعه‌ی اپ متا، فقط حساب‌هایی که به‌عنوان
        Tester اضافه شده‌اند می‌توانند متصل شوند.
      </div>

      <h2 className="mb-3 font-semibold">اکانت‌های متصل</h2>
      {loading ? (
        <div className="grid gap-4 sm:grid-cols-2">
          <Skeleton className="h-36" />
          <Skeleton className="h-36" />
        </div>
      ) : accounts.length === 0 ? (
        <EmptyState
          title="هنوز اکانتی متصل نشده"
          description="با یکی از دو روش رسمی بالا، اولین اکانت خود را متصل کنید. لیست همین‌جا فقط با داده‌ی واقعی پر می‌شود."
        />
      ) : (
        <div className="grid gap-4 sm:grid-cols-2">
          {accounts.map((account) => (
            <Card key={account.id}>
              <div className="flex items-start justify-between gap-2">
                <div className="flex items-center gap-3">
                  <div className="flex h-11 w-11 items-center justify-center rounded-full bg-gradient-to-br from-fuchsia-600 via-rose-500 to-amber-400 text-white">
                    <InstagramIcon className="h-5 w-5" />
                  </div>
                  <div>
                    <p className="font-semibold" dir="ltr">
                      @{account.username}
                    </p>
                    <p className="text-xs text-zinc-500">{account.display_name ?? account.external_account_id ?? ""}</p>
                  </div>
                </div>
                <Badge tone={STATUS_TONE[account.status] ?? "zinc"}>
                  {STATUS_LABEL[account.status] ?? account.status}
                </Badge>
              </div>
              <dl className="mt-4 grid grid-cols-3 gap-2 text-center text-xs text-zinc-500">
                <div className="rounded-xl bg-zinc-50 py-2 dark:bg-zinc-800/60">
                  <dt>نوع حساب</dt>
                  <dd className="mt-1 font-semibold text-zinc-800 dark:text-zinc-200">
                    {account.account_type ?? "—"}
                  </dd>
                </div>
                <div className="rounded-xl bg-zinc-50 py-2 dark:bg-zinc-800/60">
                  <dt>دنبال‌کننده</dt>
                  <dd className="mt-1 font-semibold text-zinc-800 dark:text-zinc-200">
                    {account.followers_count ?? "—"}
                  </dd>
                </div>
                <div className="rounded-xl bg-zinc-50 py-2 dark:bg-zinc-800/60">
                  <dt>پست‌ها</dt>
                  <dd className="mt-1 font-semibold text-zinc-800 dark:text-zinc-200">
                    {account.media_count ?? "—"}
                  </dd>
                </div>
              </dl>
              <div className="mt-4 flex items-center gap-2">
                <Button
                  variant="secondary"
                  className="text-xs"
                  busy={busyId === account.id}
                  onClick={() => void onRefreshToken(account)}
                >
                  تازه‌سازی توکن
                </Button>
                <Button
                  variant="danger"
                  className="text-xs"
                  busy={busyId === account.id}
                  onClick={() => void onDisconnect(account)}
                >
                  قطع اتصال
                </Button>
              </div>
            </Card>
          ))}
        </div>
      )}
    </AppShell>
  );
}
