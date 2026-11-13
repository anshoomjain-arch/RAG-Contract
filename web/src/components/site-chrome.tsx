"use client";

import Link from "next/link";
import { useTheme } from "next-themes";
import { FileText, Monitor, Moon, Sun } from "lucide-react";
import { useSyncExternalStore } from "react";

import { Button } from "@/components/ui/button";

const NEXT_THEME = { system: "light", light: "dark", dark: "system" } as const;

function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  // The theme is only known in the browser; render the neutral icon on the server and during hydration.
  const mounted = useSyncExternalStore(() => () => {}, () => true, () => false);
  const current = (mounted ? theme : "system") as keyof typeof NEXT_THEME;
  const Icon = current === "light" ? Sun : current === "dark" ? Moon : Monitor;
  return (
    <Button variant="ghost" size="icon" onClick={() => setTheme(NEXT_THEME[current] ?? "system")}
            aria-label={`Theme: ${current}. Switch to ${NEXT_THEME[current]}`} title={`Theme: ${current}`}>
      <Icon />
    </Button>
  );
}

export function SiteHeader() {
  return (
    <header className="border-b bg-background/80 backdrop-blur supports-backdrop-filter:bg-background/60">
      <div className="mx-auto flex h-14 w-full max-w-6xl items-center justify-between px-4 sm:px-6">
        <Link href="/" className="flex items-center gap-2 rounded-md font-heading text-lg font-semibold
                                  focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
          <FileText className="size-5 text-muted-foreground" aria-hidden />
          RAG-Contract
        </Link>
        <ThemeToggle />
      </div>
    </header>
  );
}

export function SiteFooter() {
  return (
    <footer className="border-t">
      <p className="mx-auto max-w-6xl px-4 py-4 text-xs text-muted-foreground sm:px-6">
        AI-generated content. Verify against the source document. This is not legal advice.
      </p>
    </footer>
  );
}
