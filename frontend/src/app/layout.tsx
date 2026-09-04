import type { Metadata } from "next";
import type { ReactNode } from "react";

import { productCopy } from "@/lib/product-copy";

import "./globals.css";

export const metadata: Metadata = {
  title: productCopy.name,
  description: productCopy.description,
};

export default function RootLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
