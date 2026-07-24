import { PDFDocument, StandardFonts, rgb } from 'pdf-lib';

const FONT_SIZE = 8;
const MARGIN = 18;
const GRAY = rgb(0.55, 0.55, 0.55);
const OPACITY = 0.45;
// Stamp a spread sample of pages instead of every page, to stay well within
// the Cloudflare Pages/Workers CPU budget. Honest caveat: the dominant cost is
// PDFDocument.load()/save() (a whole-document parse + re-serialize), which is
// fixed regardless of how many pages we draw on — so this trims the per-page
// drawText work, not that fixed cost. The first and last pages are always
// stamped (they're the obvious crop targets) and interior pages every STRIDE,
// so the mark stays spread through the book and can't be removed by dropping
// an edge page. Override the stride per-deploy with env.WATERMARK_PAGE_STRIDE.
const DEFAULT_STRIDE = 10;

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
