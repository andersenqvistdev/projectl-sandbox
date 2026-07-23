/**
 * UTC calendar-date formatting shared by purchase-date and delivery-date
 * stamping — always YYYY-MM-DD, never a localized or time-of-day format.
 */
export function formatDateUTC(unixSeconds) {
  const ms = Number.isFinite(unixSeconds) ? unixSeconds * 1000 : Date.now();
  return new Date(ms).toISOString().slice(0, 10);
}
