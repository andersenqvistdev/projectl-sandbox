// Cloudflare Pages Function for GET /api/grant.
//
// Path A deployment: this rides the Pages + GitHub build alongside the static
// storefront — no separate `wrangler deploy`. It is a thin adapter from the
// Pages `onRequest` signature (a context object) to the worker's existing
// (request, env) gate handler, so the entitlement/verification/ledger logic
// lives in exactly one place (worker/src/index.js) and is exercised by the
// same tests. Only GET is exported, so Pages returns 405 for other methods.
import { handleGrant } from '../../worker/src/index.js';

export const onRequestGet = ({ request, env }) => handleGrant(request, env);
