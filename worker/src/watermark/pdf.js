import { PDFDocument, StandardFonts, rgb } from 'pdf-lib';

const FONT_SIZE = 9;
const MARGIN = 22;
const GRAY = rgb(0.4, 0.4, 0.4);
const OPACITY = 0.6;
// Stamp EVERY page by default (stride 1). A watermark that can't be seen isn't
// a deterrent, and — the honest reason the earlier "lighter" version was a bad
// trade — the dominant Worker CPU cost is PDFDocument.load()/save() (a whole-
// document parse + re-serialize), which is FIXED regardless of how many pages
// we draw on. Skipping pages barely saved CPU but made the mark easy to miss.
// The stride is still overridable per-deploy via env.WATERMARK_PAGE_STRIDE for
// anyone who deliberately wants it thinned; the first and last pages are always
// stamped so a thinned mark still can't be cropped off an edge.
const DEFAULT_STRIDE = 1;

function shouldStamp(index, lastIndex, stride) {
  return index === 0 || index === lastIndex || index % stride === 0;
}

/**
 * Stamps a low-opacity footer line on a spread sample of pages. The font is
 * embedded once (not per page) — every stamped page shares the same font and
 * text, so re-embedding would be pure O(pages) overhead.
 */
export async function stamp(bytes, watermarkText, options = {}) {
  const stride = Number.parseInt(options.stride, 10) > 0 ? Number.parseInt(options.stride, 10) : DEFAULT_STRIDE;
  const pdfDoc = await PDFDocument.load(bytes);
  const font = await pdfDoc.embedFont(StandardFonts.Helvetica);
  const pages = pdfDoc.getPages();
  const lastIndex = pages.length - 1;
  const textWidth = font.widthOfTextAtSize(watermarkText, FONT_SIZE);

  pages.forEach((page, index) => {
    if (!shouldStamp(index, lastIndex, stride)) return;
    const { width } = page.getSize();
    page.drawText(watermarkText, {
      x: Math.max(MARGIN, (width - textWidth) / 2),
      y: MARGIN,
      size: FONT_SIZE,
      font,
      color: GRAY,
      opacity: OPACITY,
    });
  });

  return pdfDoc.save();
}
