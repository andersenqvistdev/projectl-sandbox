const BANNER_STYLE =
  'margin:2em 0 0;padding:0.6em 1em;border-top:2px solid #999;' +
  "font:12px/1.4 system-ui,-apple-system,sans-serif;color:#555;text-align:center;";

/**
 * Injects a fixed footer banner carrying the watermark text just before
 * </body>. Falls back to appending at the end of the document if no </body>
 * tag is found (defensive — the sample and real HTML deliverable both have
 * one).
 */
export async function stamp(bytes, watermarkText) {
  const html = new TextDecoder().decode(bytes);
  const banner = `<div style="${BANNER_STYLE}">${escapeHtml(watermarkText)}</div>`;
  const stamped = /<\/body>/i.test(html)
    ? html.replace(/<\/body>/i, `${banner}</body>`)
    : `${html}${banner}`;
  return new TextEncoder().encode(stamped);
}

function escapeHtml(text) {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
