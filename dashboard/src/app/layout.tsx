import type { Metadata } from "next";
import { cookies } from "next/headers";
import { Suspense } from "react";
import { Geist, Geist_Mono } from "next/font/google";
import { Nav } from "@/components/nav";
import { AppSidebar } from "@/components/app-sidebar";
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar";
import { TooltipProvider } from "@/components/ui/tooltip";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Xupertrade Dashboard",
  description: "Trading bot monitoring dashboard",
};

export default async function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  // Read the persisted sidebar collapse state server-side so the
  // first paint matches what the operator left it on (no
  // expand-then-collapse flash on reload). Cookie is written
  // client-side by `SidebarProvider.setOpen`. Default = open.
  // Copilot review fix on PR #103.
  const cookieStore = await cookies();
  const sidebarCookie = cookieStore.get("sidebar_state")?.value;
  const sidebarDefaultOpen = sidebarCookie !== "false";

  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} dark h-full antialiased`}
    >
      <body className="min-h-full">
        <TooltipProvider>
          <SidebarProvider defaultOpen={sidebarDefaultOpen}>
            <Suspense>
              <AppSidebar />
            </Suspense>
            <SidebarInset className="flex min-h-screen flex-col">
              {/* TODO PR C: remove Nav after sidebar cutover */}
              <Suspense>
                <Nav />
              </Suspense>
              <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-6">
                {children}
              </main>
            </SidebarInset>
          </SidebarProvider>
        </TooltipProvider>
      </body>
    </html>
  );
}
