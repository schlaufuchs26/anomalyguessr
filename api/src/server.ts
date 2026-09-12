import path from "node:path";
import { Generator } from "./generate.ts";
import { handle } from "./routes.ts";
import { Store } from "./store.ts";

/**
 * AnomalyGuessr backend (tickets #1171, #1261). Owns the AnomalyGuessr API
 * under /anomalyguessr/api and reads/writes the same data/anomalyguessr files
 * the Python pipeline writes, so the queue, the pipeline and this service stay
 * consistent. nginx proxies /anomalyguessr/api/ here; the game + queue UI call
 * these URLs directly. Since #1261 the service lives in the game repo
 * (api/), next to the frontend, the Python pipeline (pipeline/) and the data
 * (data/anomalyguessr, untracked).
 *
 * Env (all optional; the defaults work from a checkout of this repo):
 *   ANOMALYGUESSR_DATA_DIR      queue data dir (default <repo>/data/anomalyguessr)
 *   ANOMALYGUESSR_PIPELINE_DIR  python pipeline dir (default <repo>/pipeline):
 *                               where POST /generate finds ag_generate.py
 *   ANOMALYGUESSR_ENV_FILE      .env with the model API keys (default
 *                               /home/exedev/fuchs/.env: secrets deliberately
 *                               stay in the fuchs repo, this one is public)
 *   ANOMALYGUESSR_LOG_FILE      generator log (default
 *                               /home/exedev/fuchs/logs/anomalyguessr-generate.log,
 *                               the same file the daily cron appends to)
 *   PORT                        listen port (default 8646)
 */
const repoRoot = path.resolve(import.meta.dir, "../..");
const dataDir =
  process.env.ANOMALYGUESSR_DATA_DIR ??
  path.join(repoRoot, "data", "anomalyguessr");
const pipelineDir =
  process.env.ANOMALYGUESSR_PIPELINE_DIR ?? path.join(repoRoot, "pipeline");
const envFile = process.env.ANOMALYGUESSR_ENV_FILE ?? "/home/exedev/fuchs/.env";
const logFile =
  process.env.ANOMALYGUESSR_LOG_FILE ??
  "/home/exedev/fuchs/logs/anomalyguessr-generate.log";
const port = Number(process.env.PORT ?? 8646);

const store = new Store(dataDir);
const env = {
  store,
  dataDir,
  generator: new Generator({ store, pipelineDir, envFile, logFile }),
};

Bun.serve({
  port,
  fetch(req) {
    const url = new URL(req.url);
    const p = url.pathname;
    // Only the API prefix is ours; everything else is not found.
    const prefix = "/anomalyguessr/api";
    if (p === prefix || p.startsWith(`${prefix}/`)) {
      const rest = p.slice(prefix.length).replace(/^\/+/, "");
      return handle(env, req.method, rest, req);
    }
    return new Response("not found", { status: 404 });
  },
});

console.log(`anomalyguessr-api listening on :${port} (data: ${dataDir})`);
