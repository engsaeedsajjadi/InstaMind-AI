"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { Button, Card, Spinner } from "@/components/ui";

/**
 * Landing target of the backend's OAuth callback redirect:
 *   /settings/instagram?status=connected&username=...
 *   /settings/instagram?status=failed&reason=...
 */
function OAuthResult() {
  const params = useSearchParams();
  const status = params.get("status");
  const username = params.get("username");
  const reason = params.get("reason");

  const connected = status === "connected";

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <Card className="w-full max-w-md text-center">
        <div
          className={`mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full text-xl ${
            connected
              ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/60 dark:text-emerald-300"
              : "bg-rose-100 text-rose-700 dark:bg-rose-900/60 dark:text-rose-300"
          }`}
          aria-hidden="true"
        >
          {connected ? "✓" : "✕"}
        </div>
        <h1 className="text-lg font-bold">
          {connected ? "اینستاگرام متصل شد" : "اتصال ناموفق بود"}
        </h1>
        <p className="mt-2 text-sm leading-6 text-zinc-500">
          {connected ? (
            <>
              اکانت <b dir="ltr">@{username}</b> با موفقیت متصل شد و توکن آن به‌صورت رمزنگاری‌شده
              ذخیره شد.
            </>
          ) : (
            <>متا اتصال را کامل نکرد{reason ? ` (${reason})` : ""}. دوباره تلاش کنید.</>
          )}
        </p>
        <Link href="/instagram">
          <Button className="mt-6 w-full">{connected ? "مشاهده‌ی اکانت‌ها" : "بازگشت و تلاش مجدد"}</Button>
        </Link>
      </Card>
    </div>
  );
}

export default function InstagramSettingsPage() {
  return (
    <Suspense
      fallback={
        <div className="flex min-h-screen items-center justify-center">
          <Spinner className="h-7 w-7 text-brand-600" />
        </div>
      }
    >
      <OAuthResult />
    </Suspense>
  );
}
