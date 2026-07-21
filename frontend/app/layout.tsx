import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Change Detection Agent",
  description:
    "Give it a URL → it snapshots the page, compares against last visit, and reports what changed and why it matters.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
