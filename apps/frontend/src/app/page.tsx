import Link from "next/link";

const FEATURES = [
  {
    title: "اتصال رسمی اینستاگرام",
    body: "فقط از طریق APIهای رسمی متا — بدون پسورد، بدون اسکرپینگ. توکن‌ها رمزنگاری‌شده ذخیره می‌شوند.",
  },
  {
    title: "استودیو محتوا و زمان‌بندی",
    body: "ساخت پست، ریلز، کاروسل و استوری با بازبینی و تأیید تیمی و تقویم انتشار.",
  },
  {
    title: "انتشار قابل‌اعتماد",
    body: "صف انتشار با idempotency و تلاش مجدد خودکار؛ هیچ پستی دوبار منتشر نمی‌شود.",
  },
  {
    title: "هوش مصنوعی با کنترل هزینه",
    body: "پیشنهاد کپشن و برنامه‌ی محتوا با سقف بودجه‌ی هر ورک‌اسپیس و ثبت کامل هزینه.",
  },
];

export default function LandingPage() {
  return (
    <div className="flex min-h-screen flex-col">
      <header className="border-b border-zinc-200 dark:border-zinc-800">
        <div className="mx-auto flex h-14 max-w-5xl items-center px-4">
          <span className="flex items-center gap-2 font-bold">
            <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-gradient-to-br from-fuchsia-600 via-rose-500 to-amber-400 text-sm text-white">
              IM
            </span>
            اینستامایند
          </span>
          <nav className="ms-auto flex items-center gap-2">
            <Link href="/login" className="rounded-lg px-3 py-1.5 text-sm hover:bg-zinc-100 dark:hover:bg-zinc-800">
              ورود
            </Link>
            <Link
              href="/register"
              className="rounded-xl bg-brand-600 px-4 py-2 text-sm font-medium text-white hover:bg-brand-700"
            >
              شروع رایگان
            </Link>
          </nav>
        </div>
      </header>

      <main className="mx-auto w-full max-w-5xl flex-1 px-4">
        <section className="py-16 text-center">
          <span className="rounded-full bg-brand-100 px-3 py-1 text-xs font-medium text-brand-800 dark:bg-brand-950 dark:text-brand-200">
            مبتنی بر APIهای رسمی متا
          </span>
          <h1 className="mt-6 text-3xl font-extrabold leading-tight sm:text-4xl">
            مدیریت حرفه‌ای اینستاگرام،
            <br className="sm:hidden" /> امن و شفاف
          </h1>
          <p className="mx-auto mt-4 max-w-xl text-zinc-600 dark:text-zinc-400">
            محتوا بسازید، زمان‌بندی کنید و منتشر کنید؛ بدون هیچ داده‌ی ساختگی. هر معیاری که متا
            ارائه ندهد، به‌جای عدد جعلی «بدون داده» نمایش داده می‌شود.
          </p>
          <div className="mt-8 flex items-center justify-center gap-3">
            <Link
              href="/register"
              className="rounded-xl bg-brand-600 px-6 py-3 text-sm font-semibold text-white hover:bg-brand-700"
            >
              ساخت حساب
            </Link>
            <Link
              href="/login"
              className="rounded-xl border border-zinc-300 px-6 py-3 text-sm font-semibold hover:bg-zinc-100 dark:border-zinc-700 dark:hover:bg-zinc-800"
            >
              ورود به حساب
            </Link>
          </div>
        </section>

        <section className="grid gap-4 pb-16 sm:grid-cols-2">
          {FEATURES.map((feature) => (
            <div
              key={feature.title}
              className="rounded-2xl border border-zinc-200 bg-white p-5 dark:border-zinc-800 dark:bg-zinc-900"
            >
              <h2 className="font-semibold">{feature.title}</h2>
              <p className="mt-2 text-sm leading-6 text-zinc-600 dark:text-zinc-400">{feature.body}</p>
            </div>
          ))}
        </section>

        <section className="mb-16 rounded-2xl border border-amber-200 bg-amber-50 p-5 text-sm leading-6 text-amber-900 dark:border-amber-800 dark:bg-amber-950/60 dark:text-amber-100">
          <strong>پیش‌نیاز اتصال:</strong> برای وصل‌کردن اینستاگرام، حساب شما باید از نوع
          <b> Business یا Creator </b>
          باشد (تبدیل در تنظیمات اینستاگرام رایگان است). اتصال از طریق صفحه‌ی ورود رسمی متا انجام
          می‌شود و رمز اینستاگرام شما هرگز به این سامانه داده نمی‌شود.
        </section>
      </main>

      <footer className="border-t border-zinc-200 py-6 text-center text-xs text-zinc-500 dark:border-zinc-800">
        اینستامایند — نسخه‌ی ۰.۱ (پایه)
      </footer>
    </div>
  );
}
