export type BotChatMode = 'canonical' | 'task';

export function buildBotChatHref(
  pathname: string,
  search: string,
  openedId: string,
  options: { mode?: BotChatMode; profile?: string } = {},
): string {
  if (typeof openedId !== 'string' || openedId.trim().length === 0) throw new Error('blank openedId');
  const cleanPath = pathname.split('#')[0];
  const params = new URLSearchParams(search || '');
  params.set('chatSession', openedId.trim());
  if (options.mode) params.set('chatMode', options.mode);
  else params.delete('chatMode');
  if (options.profile?.trim()) params.set('botProfile', options.profile.trim());
  else params.delete('botProfile');
  const qs = params.toString();
  return cleanPath + (qs ? '?' + qs : '');
}
