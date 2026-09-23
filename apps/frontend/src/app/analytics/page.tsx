"use client";

import { useEffect, useMemo, useState } from "react";
import { AppShell } from "@/components/shell";
import { fetchAnalytics, type AnalyticsRow } from "@/lib/api";

export default function AnalyticsPage() {
  const [rows,setRows]=useState<AnalyticsRow[]>([]);
  useEffect(()=>{void fetchAnalytics().then(setRows)},[]);
  const totals=useMemo(()=>rows.reduce<Record<string,number>>((a,r)=>{if(typeof r.value==="number")a[r.metric]=(a[r.metric]||0)+r.value;return a},{}),[rows]);
  return <AppShell><h1 className="mb-5 text-xl font-bold">Analytics</h1><div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">{Object.entries(totals).map(([k,v])=><div key={k} className="rounded-2xl border bg-white p-5 dark:bg-zinc-900"><div className="text-sm text-zinc-500">{k}</div><div className="mt-2 text-3xl font-bold">{Math.round(v).toLocaleString()}</div></div>)}</div><div className="mt-5 rounded-2xl border bg-white p-4 dark:bg-zinc-900"><p className="text-sm text-zinc-500">داده‌ها فقط از Instagram Insights API نمایش داده می‌شوند؛ مقدار ناموجود صفر فرض نمی‌شود.</p></div></AppShell>;
}
