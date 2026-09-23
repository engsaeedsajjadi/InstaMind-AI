"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";

import { useToast } from "@/components/toaster";
import { Button, Card, Field } from "@/components/ui";
import { ApiError, fetchWorkspaces, isAuthenticated, login, setWorkspaceId } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const toast = useToast();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (isAuthenticated()) router.replace("/dashboard");
  }, [router]);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      await login(email, password);
      const workspaces = await fetchWorkspaces();
      setWorkspaceId(workspaces[0]?.id ?? null);
      toast.push("خوش آمدید!", "success");
      router.replace("/dashboard");
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "ورود ناموفق بود.";
      toast.push(message, "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <Card className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <span className="mx-auto mb-3 flex h-11 w-11 items-center justify-center rounded-2xl bg-gradient-to-br from-fuchsia-600 via-rose-500 to-amber-400 text-white">
            IM
          </span>
          <h1 className="text-lg font-bold">ورود به اینستامایند</h1>
          <p className="mt-1 text-sm text-zinc-500">حساب خود را مدیریت کنید</p>
        </div>
        <form onSubmit={onSubmit} className="flex flex-col gap-4">
          <Field
            label="ایمیل"
            type="email"
            dir="ltr"
            required
            autoComplete="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            placeholder="you@example.com"
          />
          <Field
            label="رمز عبور"
            type="password"
            dir="ltr"
            required
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            placeholder="••••••••"
          />
          <Button type="submit" busy={busy}>
            ورود
          </Button>
        </form>
        <p className="mt-5 text-center text-sm text-zinc-500">
          حساب ندارید؟{" "}
          <Link href="/register" className="font-medium text-brand-600 hover:underline">
            ثبت‌نام
          </Link>
        </p>
      </Card>
    </div>
  );
}
