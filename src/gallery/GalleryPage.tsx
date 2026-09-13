import { useQuery } from "@tanstack/react-query";
import React from "react";
import {
  agUrl,
  FILTER_LABELS,
  type Filter,
  getJSON,
  QUERY_KEY,
  sortRejectedScenes,
  type TDList,
  type TDScene,
} from "./api";
import { SceneCard } from "./SceneCard";
import { SceneModal } from "./SceneModal";

/** AnomalyGuessr gallery management (ticket #1113; standalone dev page since
 *  ticket #1143; renamed from the queue page in ticket #1204): browse the
 *  scene pool, reject/restore scenes and leave per-scene feedback for the
 *  generation pipeline. One vocabulary since ticket #1205: the moderation
 *  verdict (Accepted/Rejected/Unmoderated); the ship state is data.
 *  The landing view (ticket #1208) is the accepted scenes in the
 *  pipeline's own daily pick order (the API's dailyOrder), with the leading
 *  dailyCount cards marked as the upcoming daily set; Rejected and
 *  Unmoderated keep their own chips. Read-only against the game: no live
 *  generation here.
 *  Served as its own app at /anomalyguessr/gallery/ on fuchs.science (its own
 *  entry build in this repo since ticket #1434). The top-right hamburger and
 *  the all-photos overview it opened (ticket #1162) are gone since ticket
 *  #1443: the grid itself is the overview. */
export function GalleryPage() {
  const [filter, setFilter] = React.useState<Filter>("daily");
  const [open, setOpen] = React.useState<TDScene | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: QUERY_KEY,
    queryFn: () => getJSON<TDList>(agUrl("scenes")),
  });

  const scenes = data?.scenes ?? [];
  const byId = new Map(scenes.map((s) => [s.id, s]));
  const dailyCount = data?.dailyCount ?? 5;
  // The API owns the daily pick order (#1208); the UI only consumes it. A
  // server predating the field (needs /restart) degrades to the API's list
  // order over accepted scenes instead of re-deriving the rule here.
  const dailyOrder =
    data?.dailyOrder ??
    scenes.filter((s) => s.moderation === "accepted").map((s) => s.id);
  const daily = dailyOrder
    .map((id) => byId.get(id))
    .filter((s): s is TDScene => s !== undefined);

  const filtered =
    filter === "daily"
      ? daily
      : filter === "rejected"
        ? sortRejectedScenes(scenes.filter((s) => s.moderation === "rejected"))
        : scenes.filter((s) => s.moderation === "unmoderated");

  const counts: Record<Filter, number> = {
    daily: daily.length,
    rejected: scenes.filter((s) => s.moderation === "rejected").length,
    unmoderated: scenes.filter((s) => s.moderation === "unmoderated").length,
  };

  return (
    <div className="td-page">
      <div className="td-header">
        <div>
          <h2>AnomalyGuessr gallery</h2>
          <p className="td-subtitle">
            Scene pool behind the daily AnomalyGuessr quiz; rejected scenes +
            comments are read by the generation pipeline (never shown again,
            never re-added).
          </p>
        </div>
      </div>

      <div className="td-toolbar">
        {(["daily", "rejected", "unmoderated"] as const).map((f) => (
          <button
            key={f}
            type="button"
            className={`td-filter${filter === f ? " active" : ""}`}
            onClick={() => setFilter(f)}
            data-testid={`td-filter-${f}`}
          >
            {FILTER_LABELS[f]}({counts[f]})
          </button>
        ))}
      </div>

      {error ? (
        <div className="loading td-error">Failed to load: {String(error)}</div>
      ) : null}
      {isLoading || !data ? (
        <div className="loading">Loading...</div>
      ) : filter === "daily" ? (
        daily.length === 0 ? (
          <div className="td-empty">
            No accepted scenes yet; moderate scenes to fill the daily.
          </div>
        ) : (
          <>
            <section className="td-section">
              <h3 className="td-section-title">Upcoming daily</h3>
              <div className="td-grid">
                {daily.slice(0, dailyCount).map((s, i) => (
                  <SceneCard
                    key={s.id}
                    scene={s}
                    onShow={setOpen}
                    dailyRank={i + 1}
                  />
                ))}
              </div>
            </section>
            {daily.length > dailyCount ? (
              <section className="td-section">
                <h3 className="td-section-title">
                  Back catalogue · least recently shown first
                </h3>
                <div className="td-grid">
                  {daily.slice(dailyCount).map((s) => (
                    <SceneCard key={s.id} scene={s} onShow={setOpen} />
                  ))}
                </div>
              </section>
            ) : null}
          </>
        )
      ) : filtered.length === 0 ? (
        <div className="td-empty">No scenes in this view.</div>
      ) : (
        <div className="td-grid">
          {filtered.map((s) => (
            <SceneCard key={s.id} scene={s} onShow={setOpen} />
          ))}
        </div>
      )}

      {open ? <SceneModal scene={open} onClose={() => setOpen(null)} /> : null}
    </div>
  );
}
