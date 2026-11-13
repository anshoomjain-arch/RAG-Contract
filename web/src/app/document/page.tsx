import { Suspense } from "react";

import { Workspace } from "@/components/workspace";
import { Skeleton } from "@/components/ui/skeleton";

// /document?id=<uuid>: a query parameter rather than /documents/[id], so the site can be a static export
// (docs/DECISIONS.md, Phase 7). The workspace reads the ID on the client.
export default function DocumentPage() {
  return (
    <Suspense fallback={<Skeleton className="h-64 w-full" />}>
      <Workspace />
    </Suspense>
  );
}
