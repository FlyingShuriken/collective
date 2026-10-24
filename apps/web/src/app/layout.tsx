import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Orbyte Collective",
  description: "Restocking with clear costs and your approval.",
  manifest: "/manifest.webmanifest",
  icons: { icon: "/icons/icon-192.png", apple: "/icons/icon-192.png" },
  appleWebApp: { capable: true, title: "Orbyte Collective", statusBarStyle: "default" },
};

export const viewport: Viewport = { themeColor: "#183d34" };

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className="h-full antialiased"
    >
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
