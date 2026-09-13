// biome-ignore-all lint/a11y/noStaticElementInteractions: the lightbox closes
// on a click on the backdrop (the panel swallows its own clicks). Every
// dismissal is also reachable through the ✕ button, so the overlay click stays
// a mouse convenience on a role="presentation" element.
// API payload types (mirror the game repo's api/src/types.ts, the
// single AnomalyGuessr backend since #1242); kept local so this standalone
// overview does not need the page's full types.
interface TDScene {
  id: string;
  title: string;
  place: string;
  year: string;
  moderation: "accepted" | "rejected" | "unmoderated";
  images: { edited: string; original: string; audit?: string };
}

const MOD_LABELS: Record<TDScene["moderation"], string> = {
  accepted: "Accepted",
  rejected: "Rejected",
  unmoderated: "Unmoderated",
};

/**
 * All-photos overview behind the hamburger menu (ticket #1162): a full-screen
 * thumbnail grid of every queue scene, whatever its moderation verdict,
 * letting Evan browse the whole library at a glance and jump to any scene.
 * Clicking a thumbnail closes the overview and jumps to that scene; clicking
 * the backdrop or the ✕ button dismisses it. Each thumbnail carries its
 * moderation verdict badge (ticket #1163) and nothing else since ticket
 * #1205 dropped the Unshown/Shown/Excluded vocabulary. Reuses the same GET
 * /scenes payload as the main grid, so it needs no extra API call.
 */
export function AllPhotosOverview({
  scenes,
  onClose,
  onSelect,
}: {
  scenes: TDScene[];
  onClose: () => void;
  onSelect: (id: string) => void;
}) {
  return (
    <div
      className="td-overview-overlay"
      role="presentation"
      onClick={(e) => {
        // Dismiss on the backdrop only; the panel stays a plain div.
        if (e.target === e.currentTarget) onClose();
      }}
      data-testid="td-overview-overlay"
    >
      <div
        className="td-overview"
        role="dialog"
        aria-modal="true"
        aria-label="All photos"
      >
        <div className="td-overview-head">
          <h3 className="td-overview-title">All photos · {scenes.length}</h3>
          <button
            type="button"
            className="modal-close"
            onClick={onClose}
            data-testid="td-overview-close"
          >
            ✕
          </button>
        </div>
        <div className="td-overview-grid">
          {scenes.map((s) => (
            <button
              key={s.id}
              type="button"
              className="td-overview-item"
              onClick={() => onSelect(s.id)}
              data-testid={`td-overview-item-${s.id}`}
              title={`${s.title} · ${s.place}, ${s.year} (${
                MOD_LABELS[s.moderation]
              })`}
            >
              <img src={s.images.edited} alt="" loading="lazy" />
              <span
                className={`td-overview-badge td-mod-${s.moderation}`}
                data-testid={`td-overview-mod-${s.id}`}
              >
                {MOD_LABELS[s.moderation]}
              </span>
              <span className="td-overview-title2">{s.title}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
