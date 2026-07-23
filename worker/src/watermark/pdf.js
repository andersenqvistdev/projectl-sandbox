import { PDFDocument, StandardFonts, rgb } from 'pdf-lib';

const FONT_SIZE = 8;
const MARGIN = 18;
const GRAY = rgb(0.55, 0.55, 0.55);
const OPACITY = 0.45;

/**
 * Stamps a low-opacity footer line on every page. The font is embedded once
 * (not per page) — with a book-sized PDF, re-embedding per page is the kind
 * of O(pages) setup cost that adds up against Worker CPU limits for no
 * benefit, since every page shares the same font and text.
 */
export async function stamp(bytes, watermarkText) {
  const pdfDoc = await PDFDocument.load(bytes);
  const font = await pdfDoc.embedFont(StandardFonts.Helvetica);

  for (const page of pdfDoc.getPages()) {
    const { width } = page.getSize();
    const textWidth = font.widthOfTextAtSize(watermarkText, FONT_SIZE);
    page.drawText(watermarkText, {
      x: Math.max(MARGIN, (width - textWidth) / 2),
      y: MARGIN,
      size: FONT_SIZE,
      font,
      color: GRAY,
      opacity: OPACITY,
    });
  }

  return pdfDoc.save();
}
