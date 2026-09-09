export function buildBotChatHref(pathname: string, search: string, openedId: string): string {
  if (typeof openedId !== 'string' || openedId.trim().length === 0) throw new Error('blank openedId');
  const cleanPath = pathname.split('#')[0];
  const params = new URLSearchParams(search || '');
  params.set('chatSession', openedId.trim());
  const qs = params.toString();
  return cleanPath + (qs ? '?' + qs : '');
}
