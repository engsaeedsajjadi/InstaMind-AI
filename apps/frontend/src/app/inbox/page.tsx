"use client";

import { useEffect, useState } from "react";
import { AppShell } from "@/components/shell";
import { fetchConversations, fetchMessages, sendMessage, type ConversationRow, type MessageRow } from "@/lib/api";

export default function InboxPage() {
  const [rows, setRows] = useState<ConversationRow[]>([]);
  const [active, setActive] = useState<ConversationRow | null>(null);
  const [messages, setMessages] = useState<MessageRow[]>([]);
  const [text, setText] = useState("");
  const [loading, setLoading] = useState(true);
  async function load() {
    setLoading(true);
    try { const data = await fetchConversations(); setRows(data); if (!active && data[0]) setActive(data[0]); }
    finally { setLoading(false); }
  }
  useEffect(() => { void load(); }, []);
  useEffect(() => { if (active) void fetchMessages(active.id).then(setMessages); }, [active]);
  async function submit() {
    if (!active || !text.trim()) return;
    const sent = await sendMessage(active.id, text.trim());
    setMessages((x) => [...x, sent]); setText("");
  }
  return <AppShell><div className="grid gap-4 lg:grid-cols-[320px_1fr]">
    <section className="rounded-2xl border bg-white p-3 dark:bg-zinc-900">
      <h1 className="mb-3 text-lg font-bold">Inbox</h1>
      {loading ? <p className="p-4 text-sm text-zinc-500">در حال بارگذاری…</p> :
        rows.map((row) => <button key={row.id} onClick={() => setActive(row)} className="mb-2 w-full rounded-xl border p-3 text-right hover:bg-zinc-50 dark:hover:bg-zinc-800">
          <div className="font-semibold">{row.participant_name || row.participant_username || "مخاطب"}</div>
          <div className="text-xs text-zinc-500">{row.status} {row.needs_human ? "• نیازمند اپراتور" : ""}</div>
        </button>)}
    </section>
    <section className="flex min-h-[600px] flex-col rounded-2xl border bg-white dark:bg-zinc-900">
      <div className="border-b p-4 font-semibold">{active?.participant_name || active?.participant_username || "گفت‌وگو را انتخاب کنید"}</div>
      <div className="flex-1 space-y-2 overflow-auto p-4">{messages.map(m => <div key={m.id} className={m.direction === "OUTBOUND" ? "mr-auto max-w-[80%] rounded-2xl bg-brand-50 p-3 dark:bg-brand-950" : "ml-auto max-w-[80%] rounded-2xl bg-zinc-100 p-3 dark:bg-zinc-800"}>{m.text}</div>)}</div>
      {active && <div className="flex gap-2 border-t p-3"><input value={text} onChange={e=>setText(e.target.value)} onKeyDown={e=>{if(e.key==="Enter") void submit()}} className="flex-1 rounded-xl border px-3 py-2" placeholder="پیام…" /><button onClick={()=>void submit()} className="rounded-xl bg-brand-600 px-5 py-2 font-semibold text-white">ارسال</button></div>}
    </section>
  </div></AppShell>;
}
