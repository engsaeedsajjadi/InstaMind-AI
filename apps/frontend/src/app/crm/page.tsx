"use client";

import { useEffect, useState } from "react";
import { AppShell } from "@/components/shell";
import { fetchCustomers, updateCustomer, type CustomerRow } from "@/lib/api";

export default function CrmPage() {
  const [rows,setRows]=useState<CustomerRow[]>([]);
  useEffect(()=>{void fetchCustomers().then(setRows)},[]);
  return <AppShell><h1 className="mb-5 text-xl font-bold">CRM مشتریان</h1><div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">{rows.map(c=><article key={c.id} className="rounded-2xl border bg-white p-4 dark:bg-zinc-900">
    <div className="font-semibold">{c.display_name || c.username || "مشتری"}</div><div className="mt-1 text-sm text-zinc-500">{c.stage} • امتیاز {c.lead_score}</div>
    <div className="mt-3 flex flex-wrap gap-1">{c.tags.map(t=><span key={t} className="rounded-full bg-zinc-100 px-2 py-1 text-xs">{t}</span>)}</div>
    <button onClick={async()=>{const x=await updateCustomer(c.id,{stage:c.stage==="NEW"?"QUALIFIED":"WON"});setRows(rows.map(r=>r.id===c.id?x:r))}} className="mt-4 rounded-xl border px-3 py-2 text-sm">تغییر مرحله</button>
  </article>)}</div></AppShell>;
}
