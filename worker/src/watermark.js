/**
 * Watermark format registry.
 *
 * Mirrors the entitlement-type registry pattern in entitlements.js: the
 * download route (index.js) never branches on format by name — it looks up
 * the format in WATERMARKERS and dispatches. Adding a new deliverable format
 * means registering a stamper here; index.js does not change.
 *
 * A stamper is: async (bytes: Uint8Array, watermarkText: string) => Uint8Array
 *
 * Unlike entitlement verifiers (where an unknown type is a normal 403), an
 * unknown format at download time is a fail-closed 500 — see index.js. The
 * raw artifact must never be served un-stamped, so "no stamper registered"
 * can never fall through to returning the original bytes.
 */

export const WatermarkFormat = Object.freeze({
  PDF: 'pdf',
  HTML: 'html',
  EPUB: 'epub',
});

const WATERMARKERS = {};

export function registerWatermarker(format, stamper) {
  WATERMARKERS[format] = stamper;
}

export function getWatermarker(format) {
  return WATERMARKERS[format];
}

const EXTENSION_TO_FORMAT = {
  '.pdf': WatermarkFormat.PDF,
  '.html': WatermarkFormat.HTML,
  '.htm': WatermarkFormat.HTML,
  '.epub': WatermarkFormat.EPUB,
};

/** Detects format from an R2 object key's file extension, or null if unrecognized. */
export function detectFormat(objectKey) {
  const match = /\.[a-z0-9]+$/i.exec(objectKey);
  if (!match) return null;
  return EXTENSION_TO_FORMAT[match[0].toLowerCase()] || null;
}

const FORMAT_CONTENT_TYPES = {
  [WatermarkFormat.PDF]: 'application/pdf',
  [WatermarkFormat.HTML]: 'text/html; charset=utf-8',
  [WatermarkFormat.EPUB]: 'application/epub+zip',
};

/** Content-Type for a watermarked format's response — the stamped copy's real type. */
export function contentTypeForFormat(format) {
  return FORMAT_CONTENT_TYPES[format] || 'application/octet-stream';
}

/**
 * Builds the per-buyer watermark sentence stamped into every delivered copy.
 *
 * Privacy note: only the buyer's email *domain* is embedded in the document
 * text (e.g. "buyer at example.com"), never the full address — the
 * document itself may end up copied/shared beyond the original buyer, and
 * the full email shouldn't ride along with it. The order id (already tied
 * to the full email server-side, via the GRANTS_KV watermark record written
 * at grant time) is what lets the operator trace a leaked copy back to a
 * specific purchase.
 *
 * `buyerEmail` absent/null falls back to the order id alone, per spec.
 */
export function buildWatermarkText({ buyerEmail, orderId, purchasedAt, deliveryDate }) {
  const domain = buyerEmail && buyerEmail.includes('@') ? buyerEmail.split('@')[1] : null;
  const who = domain ? `buyer at ${domain}` : `order ${orderId}`;
  const orderPart = domain ? `, order ${orderId}` : '';
  const purchasedPart = purchasedAt ? `, purchased ${purchasedAt}` : '';
  const deliveredPart = deliveryDate ? `, delivered ${deliveryDate}` : '';
  return `Licensed to ${who}${orderPart}${purchasedPart}${deliveredPart}.`;
}
