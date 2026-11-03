import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "vreview",
  description: "Review dubbed and AI-generated video like code",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
