import { readFileSync } from 'node:fs';

const drawer = readFileSync(new URL('../src/components/ChatDrawer.tsx', import.meta.url), 'utf8');
const shell = readFileSync(new URL('../src/components/MissionControlShell.tsx', import.meta.url), 'utf8');
const groupGateway = readFileSync(new URL('../src/lib/group-gateway.ts', import.meta.url), 'utf8');

function includes(source: string, value: string, label: string) {
  if (!source.includes(value)) throw new Error(`${label}: missing ${value}`);
}
function excludes(source: string, value: string, label: string) {
  if (source.includes(value)) throw new Error(`${label}: found ${value}`);
}

includes(drawer, "chatMode?: 'general' | 'canonical' | 'task' | 'room'", 'room mode is part of drawer contract');
includes(drawer, 'useGroupRoom', 'room drawer uses group room state');
includes(drawer, '<GroupRoomView', 'room drawer mounts GroupRoomView');
includes(groupGateway, "'groups.send'", 'room composer is backed by groups.send');
includes(groupGateway, 'thread_id', 'room send includes explicit thread_id');
const roomDrawer = drawer.slice(drawer.indexOf('function GroupChatDrawer'));
excludes(roomDrawer, 'useGatewayChat(', 'room mode must not initialize canonical chat');
includes(shell, "chatSearchParams.get('chatMode') === 'room'", 'shell routes room mode');
includes(shell, 'chatRoomId', 'shell owns explicit roomId URL state');
includes(shell, 'params.delete(\'roomId\')', 'closing room clears roomId');

console.log('group chat drawer contract tests passed');
