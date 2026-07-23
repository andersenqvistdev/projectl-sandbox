import { unzipSync, zipSync } from 'fflate';

/**
 * EPUB watermarking approach: STAMP THE FIRST CONTENT DOCUMENT, not a full
 * unzip / inject-colophon-into-spine / rezip.
 *
 * A proper colophon page means editing the OPF manifest *and* spine,
 * getting EPUB2 NCX vs EPUB3 nav semantics right, and rezipping while
 * preserving the mandatory first-and-uncompressed `mimetype` entry — many
 * ways to hand back a subtly corrupt EPUB for comparatively little
 * deterrence upside over a simpler visible stamp. Instead: unzip with
 * fflate, resolve the first spine item via content.opf, inject a visible
 * watermark banner as the first element of that document's <body>, and
 * rezip every entry byte-identical except that one.
 */
export async function stamp(bytes, watermarkText) {
  const files = unzipSync(bytes);

  const containerXml = decode(files['META-INF/container.xml']);
  const opfPath = /full-path="([^"]+)"/.exec(containerXml)?.[1];
  if (!opfPath || !files[opfPath]) {
    throw new Error('watermark: EPUB container.xml has no resolvable OPF rootfile');
  }

  const opfXml = decode(files[opfPath]);
  const opfDir = opfPath.includes('/') ? opfPath.slice(0, opfPath.lastIndexOf('/') + 1) : '';

  const manifest = {};
  const itemRe = /<item\b([^>]*?)\/?>/g;
  let m;
  while ((m = itemRe.exec(opfXml))) {
    const attrs = m[1];
    const id = /\bid="([^"]+)"/.exec(attrs)?.[1];
    const href = /\bhref="([^"]+)"/.exec(attrs)?.[1];
    if (id && href) manifest[id] = href;
  }

  const firstIdref = /<itemref\b[^>]*\bidref="([^"]+)"/.exec(opfXml)?.[1];
  const firstHref = firstIdref && manifest[firstIdref];
  if (!firstHref) {
    throw new Error('watermark: EPUB spine has no resolvable first item');
  }

  const contentPath = opfDir + firstHref;
  if (!files[contentPath]) {
    throw new Error(`watermark: EPUB content document not found at ${contentPath}`);
  }

  const stampedXhtml = injectBanner(decode(files[contentPath]), watermarkText);

  const output = {};
  // The EPUB OCF spec requires `mimetype` to be the first zip entry, stored
  // (uncompressed) — preserve both properties explicitly on rezip.
  output.mimetype = [files.mimetype, { level: 0 }];
  for (const [path, data] of Object.entries(files)) {
    if (path === 'mimetype') continue;
    output[path] = path === contentPath ? encode(stampedXhtml) : data;
  }

  return zipSync(output);
}

function injectBanner(xhtml, watermarkText) {
  const banner =
    '<div style="border-bottom:1px solid #999;padding:0.5em;' +
    `font-size:0.75em;color:#666;">${escapeXml(watermarkText)}</div>`;
  if (/<body[^>]*>/i.test(xhtml)) {
    return xhtml.replace(/<body([^>]*)>/i, `<body$1>${banner}`);
  }
  return banner + xhtml;
}

function escapeXml(text) {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function decode(bytes) {
  return new TextDecoder().decode(bytes);
}

function encode(text) {
  return new TextEncoder().encode(text);
}
