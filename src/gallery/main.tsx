import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRoot } from "react-dom/client";
import { ErrorBoundary } from "./ErrorBoundary";
import { GalleryPage } from "./GalleryPage";

/**
 * Entry of the AnomalyGuessr gallery/curation app (ticket #1143), served at
 * /anomalyguessr/gallery/ on the dev instance (renamed from /queue/ in ticket
 * #1204). It lived in the fuchs-core dashboard until ticket #1434, which moved
 * it into this repo as its own entry build (`gallery.html` -> dist/gallery/).
 * The page queries the AnomalyGuessr API at /anomalyguessr/api/ through nginx;
 * this shell adds a small top bar with a link back to the playable dev
 * instance.
 */
const queryClient = new QueryClient({
  // Same defaults as the dashboard had: a query is immediately stale, so a
  // remount refetches; the slow background interval keeps an open gallery in
  // step with the generation pipeline.
  defaultOptions: { queries: { refetchInterval: 5 * 60 * 1000, staleTime: 0 } },
});

function GalleryApp() {
  return (
    <QueryClientProvider client={queryClient}>
      <div className="container">
        <header className="agq-topbar">
          <span className="agq-brand">🦊 AnomalyGuessr</span>
          <nav className="agq-nav">
            <a href="/anomalyguessr/" className="agq-link">
              ▶ Play
            </a>
            <span className="agq-current">Gallery</span>
          </nav>
        </header>
        <GalleryPage />
      </div>
    </QueryClientProvider>
  );
}

if (typeof document !== "undefined" && document.getElementById("root")) {
  const root = createRoot(document.getElementById("root") as HTMLElement);
  root.render(
    <ErrorBoundary>
      <GalleryApp />
    </ErrorBoundary>,
  );
}
