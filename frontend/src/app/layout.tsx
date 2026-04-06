import type { Metadata } from "next";
import "./globals.css";
import { AuthProvider } from "@/contexts/AuthContext";
import { Toaster } from "sonner";
import { Toaster as ShadcnToaster } from "@/components/ui/toaster";
import { I18nProvider } from "@/i18n";

export const metadata: Metadata = {
  metadataBase: new URL(
    process.env.NEXT_PUBLIC_APP_URL || "http://localhost:3000",
  ),
  title: {
    default: "Kagura Memory Cloud - Universal AI Memory Platform",
    template: "%s | Kagura Memory Cloud",
  },
  description:
    "Self-hosted, open source AI memory platform. 9 MCP tools, Neural Memory, Hybrid Search, team collaboration. Works with Claude, ChatGPT, and any MCP client.",
  keywords: [
    "AI memory",
    "MCP",
    "Claude",
    "ChatGPT",
    "context management",
    "Neural Memory",
    "team collaboration",
    "Qdrant",
    "vector database",
  ],
  authors: [{ name: "Kagura AI" }],
  creator: "Kagura AI",
  publisher: "Kagura AI",
  openGraph: {
    type: "website",
    locale: "en_US",
    url: process.env.NEXT_PUBLIC_APP_URL || "http://localhost:3000",
    title: "Kagura Memory Cloud - Universal AI Memory Platform",
    description:
      "Never lose your AI context again. Production-ready memory platform with 8 MCP tools, Neural Memory, and team collaboration.",
    siteName: "Kagura Memory Cloud",
    images: [
      {
        url: "/og-image.png",
        width: 1200,
        height: 630,
        alt: "Kagura Memory Cloud - Universal AI Memory Platform",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: "Kagura Memory Cloud - Universal AI Memory Platform",
    description:
      "Never lose your AI context again. Production-ready memory platform with 8 MCP tools, Neural Memory, and team collaboration.",
    images: ["/og-image.png"],
    creator: "@kagura_ai",
  },
  robots: {
    index: true,
    follow: true,
    googleBot: {
      index: true,
      follow: true,
      "max-video-preview": -1,
      "max-image-preview": "large",
      "max-snippet": -1,
    },
  },
  ...(process.env.NEXT_PUBLIC_GOOGLE_VERIFICATION && {
    verification: {
      google: process.env.NEXT_PUBLIC_GOOGLE_VERIFICATION,
    },
  }),
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script
          dangerouslySetInnerHTML={{
            __html: `
          try {
            var stored = localStorage.getItem('theme');
            var prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
            if (stored === 'dark' || (!stored && prefersDark)) {
              document.documentElement.classList.add('dark');
            }
          } catch (e) {}
        `,
          }}
        />
      </head>
      <body>
        <I18nProvider>
          <AuthProvider>{children}</AuthProvider>
          <Toaster richColors position="top-right" />
          <ShadcnToaster />
        </I18nProvider>
      </body>
    </html>
  );
}
