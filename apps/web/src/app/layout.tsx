import type { Metadata } from "next";
import { Inter } from "next/font/google";
import Link from "next/link";

import "./globals.css";

const inter = Inter({
  variable: "--font-inter",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "VBall",
  description: "Volleyball video analysis",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${inter.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col">
        <header className="sticky top-0 z-20 border-b border-slate-200 bg-white/95 backdrop-blur">
          <nav className="mx-auto flex h-16 w-full max-w-6xl items-center gap-8 px-4 text-[15px]">
            <Link href="/" className="flex items-center gap-2 text-xl font-bold tracking-tight text-blue-600">
              <span aria-hidden>🏐</span> VBall
            </Link>
            <Link href="/videos" className="font-medium text-slate-500 hover:text-slate-900">
              Videos
            </Link>
            <Link href="/account" className="ml-auto font-medium text-slate-500 hover:text-slate-900">
              Account
            </Link>
          </nav>
        </header>
        {children}
      </body>
    </html>
  );
}
