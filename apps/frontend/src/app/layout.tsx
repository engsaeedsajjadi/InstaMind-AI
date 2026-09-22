import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import { ToasterProvider } from "@/components/toaster";
import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "اینستامایند | مدیریت هوشمند اینستاگرام",
    template: "%s | اینستامایند",
  },
  description:
    "پلتفرم مدیریت حرفه‌ای اینستاگرام: تولید محتوا با هوش مصنوعی، زمان‌بندی و انتشار — فقط با APIهای رسمی متا.",
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#fafafa" },
    { media: "(prefers-color-scheme: dark)", color: "#09090b" },
  ],
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="fa" dir="rtl" suppressHydrationWarning>
      <head>
        {/* Font loads at runtime; the build never depends on the network. */}
        <link rel="preconnect" href="https://cdn.jsdelivr.net" />
        <link
          rel="stylesheet"
          href="https://cdn.jsdelivr.net/gh/rastikerdar/vazirmatn@v33.003/Vazirmatn-font-face.css"
        />
      </head>
      <body className="font-sans antialiased">
        <ToasterProvider>{children}</ToasterProvider>
      </body>
    </html>
  );
}
