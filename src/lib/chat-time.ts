function isSameLocalDay(left: Date, right: Date): boolean {
  return left.getFullYear() === right.getFullYear()
    && left.getMonth() === right.getMonth()
    && left.getDate() === right.getDate();
}

export function formatChatMessageTime(
  timestamp: number,
  referenceNow = Date.now(),
  locale?: string | string[],
): string {
  const date = new Date(timestamp);
  if (!Number.isFinite(date.getTime())) return '';
  const reference = new Date(referenceNow);
  if (isSameLocalDay(date, reference)) {
    return date.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' });
  }
  return date.toLocaleString(locale, { dateStyle: 'medium', timeStyle: 'short' });
}
