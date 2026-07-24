// Cloudflare Pages Function for GET /api/download.
//
// Thin adapter to the worker's existing download handler (see functions/api/
// grant.js for the rationale). The handler fetches the raw artifact from R2,
// stamps a per-buyer watermark into an in-memory copy, and streams that copy —
// it never returns the raw object. Same code, same tests, deployed via Pages.
import { handleDownload } from '../../worker/src/index.js';

export const onRequestGet = ({ request, env }) => handleDownload(request, env);
