"use client";

import { useEffect, useState } from "react";
import { AppShell } from "@/components/shell";
import { fetchComments, hideComment, replyComment, type CommentRow } from "@/lib/api";

export default function CommentsPage() {
  const [rows,setRows]=useState<CommentRow[]>([]); const [reply,setReply]=useState<Record<string,string>>({});
  useEffect(()=>{void fetchComments().then(setRows)},[]);
  return <AppShell><h1 className="mb-5 text-xl font-bold">کامنت‌ها</h1><div className="space-y-3">{rows.map(c=><article key={c.id} className="rounded-2xl border bg-white p-4 dark:bg-zinc-900">
    <div className="mb-2 flex justify-between"><b>@{c.from_username || "unknown"}</b><span className="text-xs text-zinc-500">{c.status}</span></div>
    <p className="text-sm">{c.text}</p>
    <div className="mt-3 flex gap-2"><input value={reply[c.id]||""} onChange={e=>setReply({...reply,[c.id]:e.target.value})} className="flex-1 rounded-xl border px-3 py-2" placeholder="پاسخ…" />
    <button onClick={async()=>{if(!reply[c.id]?.trim())return; const x=await replyComment(c.id,reply[c.id]);setRows(rows.map(r=>r.id===c.id?x:r));setReply({...reply,[c.id]:""})}} className="rounded-xl bg-brand-600 px-4 py-2 text-white">پاسخ</button>
    <button onClick={async()=>{const x=await hideComment(c.id);setRows(rows.map(r=>r.id===c.id?x:r))}} className="rounded-xl border px-4 py-2">مخفی</button></div>
  </article>)}</div></AppShell>;
}
