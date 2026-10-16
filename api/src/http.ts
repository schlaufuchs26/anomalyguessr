/**
 * JSON response helpers shared by the route modules (ticket #2669): the small
 * `Response` wrappers `routes.ts` and `moderate.ts` both build answers with.
 */

export function json(
  body: unknown,
  status = 200,
  extra?: Record<string, string>,
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...extra },
  });
}

export function notFound(): Response {
  return json({ error: "not found" }, 404);
}

export function badRequest(msg: string): Response {
  return json({ error: msg }, 400);
}
