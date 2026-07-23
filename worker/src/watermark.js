/**
 * Per-buyer watermarking — the download route (index.js) never serves a
 * fetched R2 object's bytes directly; every response goes through
 * watermarkArtifact() first. Each stamper takes the whole artifact in
 * memory and returns a new, stamped copy; there is deliberately no path
 * that returns the input bytes unchanged.
 *
 * Format is resolved by detectFormat() from the R2 object's content type
 * (preferred) or its key's file extension. An artifact whose format can't
 * be resolved has no stamper to run, so the caller must fail closed
 * (never fall back to serving it raw) — see index.js:handleDownload.
 */

import { PDFDocument, StandardFonts, rgb } from 'pdf-lib';
import { unzipSync, zipSync, strToU8, strFromU8 } from 'fflate';

const CONTENT_TYPE_FORMATS = {
  'application/pdf': 'pdf',
  'application/epub+zip': 'epub',
  'text/html': 'html',
};

const EXTENSION_FORMATS = { pdf: 'pdf', epub: 'epub', html: 'html', htm: 'html' };

const FORMAT_CONTENT_TYPES = {
  pdf: 'application/pdf',
  epub: 'application/epub+zip',
  html: 'text/html; charset=utf-8',
};

export function detectFormat(objectKey, contentType) {
  if (contentType) {
    const base = contentType.split(';')[0].trim().toLowerCase();
    if (CONTENT_TYPE_FORMATS[base]) return CONTENT_TYPE_FORMATS[base];
  }
  const ext = objectKey.includes('.') ? objectKey.split('.').pop().toLowerCase() : '';
  return EXTENSION_FORMATS[ext] || null;
}

export function contentTypeForFormat(format) {
  return FORMAT_CONTENT_TYPES[format] || 'application/octet-stream';
}

function obfuscateEmail(email) {
  // " at " instead of "@" so the stamp isn't a trivially scrapable mailto —
  // still fully identifying to whoever is checking a leaked copy by hand.
  return email.replace('@', ' at ');
}

/**
 * Three things are always stamped: a buyer identifier (email, obfuscated —
 * or the order id alone if no email was collected), the order id, and both
 * the purchase date and delivery date (YYYY-MM-DD, UTC).
 */
export function buildWatermarkText({ buyerEmail, orderId, purchaseDate, deliveryDate }) {
  const identifier = buyerEmail ? obfuscateEmail(buyerEmail) : `order ${orderId}`;
  const orderClause = buyerEmail ? `, order ${orderId}` : '';
  return `Licensed to ${identifier}${orderClause}, purchased ${purchaseDate}, delivered ${deliveryDate}.`;
}

function escapeHtml(text) {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// Helvetica/WinAnsi can't encode most non-Latin-1 characters (e.g. CJK,
// Cyrillic) — an email with such characters in its local part would
// otherwise make page.drawText() throw on *every* attempt, permanently
// failing that buyer's PDF download. font.encodeText() runs the same
// validation drawText does internally, so it's used here to detect that
// case up front and fall back to an ASCII-safe rendering of the same text
// rather than losing the watermark (and the download) entirely.
function pdfSafeText(font, text) {
  try {
    font.encodeText(text);
    return text;
  } catch {
    return text.replace(/[^\x20-\x7E]/g, '?');
  }
}

async function stampPdf(bytes, text) {
  const doc = await PDFDocument.load(bytes);
  const font = await doc.embedFont(StandardFonts.Helvetica);
  const safeText = pdfSafeText(font, text);
  for (const page of doc.getPages()) {
    page.drawText(safeText, {
      x: 24,
      y: 16,
      size: 7,
      font,
      color: rgb(0.45, 0.45, 0.45),
      opacity: 0.5,
    });
  }
  return doc.save();
}

function stampHtml(bytes, text) {
  const html = new TextDecoder('utf-8').decode(bytes);
  const footer =
    `<div style="margin-top:2rem;padding:0.75rem 1rem;font:12px/1.4 sans-serif;` +
    `color:#888;opacity:0.65;border-top:1px solid #ddd;">${escapeHtml(text)}</div>`;
  const stamped = /<\/body>/i.test(html) ? html.replace(/<\/body>/i, `${footer}</body>`) : `${html}${footer}`;
  return new TextEncoder().encode(stamped);
}

function escapeXml(text) {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/**
 * Adds a colophon page to the spine rather than editing existing chapter
 * text, so the watermark survives independent of book content structure.
 * The OPF's directory is resolved from META-INF/container.xml rather than
 * assumed to be "EPUB/", since that root directory name isn't part of the
 * EPUB spec (some producers use "OEBPS/" or the zip root).
 */
function stampEpub(bytes, text) {
  const files = unzipSync(bytes);
  const containerXml = files['META-INF/container.xml'] && strFromU8(files['META-INF/container.xml']);
  const rootfileMatch = containerXml && containerXml.match(/full-path="([^"]+)"/);
  if (!rootfileMatch) {
    throw new Error('EPUB missing META-INF/container.xml rootfile reference');
  }
  const opfPath = rootfileMatch[1];
  const baseDir = opfPath.includes('/') ? opfPath.slice(0, opfPath.lastIndexOf('/') + 1) : '';
  let opf = files[opfPath] && strFromU8(files[opfPath]);
  if (!opf || !opf.includes('</manifest>') || !opf.includes('</spine>')) {
    throw new Error('EPUB OPF missing manifest or spine element');
  }

  const colophonPath = `${baseDir}text/colophon.xhtml`;
  const colophonXhtml = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<!DOCTYPE html>',
    '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">',
    '<head><meta charset="utf-8" /><title>Colophon</title></head>',
    '<body epub:type="colophon"><section epub:type="colophon" class="colophon">' +
      `<p>${escapeXml(text)}</p></section></body>`,
    '</html>',
    '',
  ].join('\n');

  opf = opf.replace(
    '</manifest>',
    '  <item id="colophon_xhtml" href="text/colophon.xhtml" media-type="application/xhtml+xml" />\n</manifest>'
  );
  opf = opf.replace('</spine>', '  <itemref idref="colophon_xhtml" linear="yes" />\n</spine>');

  files[opfPath] = strToU8(opf);
  files[colophonPath] = strToU8(colophonXhtml);

  // mimetype must stay first and stored (uncompressed) per the EPUB spec.
  const ordered = {};
  if (files.mimetype) ordered.mimetype = [files.mimetype, { level: 0 }];
  for (const [name, data] of Object.entries(files)) {
    if (name === 'mimetype') continue;
    ordered[name] = data;
  }
  return zipSync(ordered);
}

export async function watermarkArtifact({ format, bytes, text }) {
  if (format === 'pdf') return stampPdf(bytes, text);
  if (format === 'html') return stampHtml(bytes, text);
  if (format === 'epub') return stampEpub(bytes, text);
  throw new Error(`unsupported watermark format: ${format}`);
}
