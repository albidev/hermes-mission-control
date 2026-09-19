import type { GatewayEvent, GatewayInteractionRequest } from './chat-protocol';

export type ClarifyInteractionContent = {
  question: string;
  choices: string[];
  multiSelect: boolean;
  questionId: string | null;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function stringChoices(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((choice): choice is string => typeof choice === 'string' && choice.trim().length > 0)
    : [];
}

function parseJsonRecord(value: unknown): Record<string, unknown> | null {
  if (isRecord(value)) return value;
  if (typeof value !== 'string' || !value.trim()) return null;
  try {
    const parsed = JSON.parse(value) as unknown;
    return isRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

/**
 * Gateway clarify events have appeared in two shapes: the native event payload
 * (`question` / `choices`) and the structured clarify result
 * (`responses[0].question` / `choices_offered`). Keep this at the interaction
 * boundary: the answer card owns this content, not the generic tool renderer.
 */
export function normalizeClarifyInteraction(payload: Record<string, unknown>): ClarifyInteractionContent {
  const nestedQuestions = Array.isArray(payload.questions) ? payload.questions : [];
  const questionEntry = nestedQuestions.find(isRecord);
  const nestedResponses = Array.isArray(payload.responses) ? payload.responses : [];
  const responseEntry = nestedResponses.find(isRecord);
  const question = typeof payload.question === 'string' && payload.question.trim()
    ? payload.question.trim()
    : typeof questionEntry?.question === 'string' && questionEntry.question.trim()
      ? questionEntry.question.trim()
      : typeof responseEntry?.question === 'string' ? responseEntry.question.trim() : '';
  const directChoices = stringChoices(payload.choices);
  const questionChoices = stringChoices(questionEntry?.choices);
  const responseChoices = stringChoices(responseEntry?.choices_offered);
  const choices = directChoices.length ? directChoices : questionChoices.length ? questionChoices : responseChoices;
  const multiSelect = payload.multi_select === true
    || questionEntry?.multi_select === true
    || responseEntry?.multi_select === true;
  const questionId = typeof questionEntry?.qid === 'string' && questionEntry.qid.trim()
    ? questionEntry.qid.trim()
    : typeof payload.question_id === 'string' && payload.question_id.trim() ? payload.question_id.trim() : null;
  return { question, choices, multiSelect, questionId };
}

/** Extract the structured clarify result when the gateway emits it as a tool event. */
export function extractClarifyToolContent(event: GatewayEvent): ClarifyInteractionContent | null {
  const payload = event.payload ?? {};
  const toolName = [payload.name, payload.tool_name, payload.tool]
    .find((value): value is string => typeof value === 'string')?.trim().toLowerCase();
  if (toolName !== 'clarify') return null;
  const candidates = [payload.args, payload.result, payload.output, payload.result_text, payload.content, payload.summary, payload];
  for (const candidate of candidates) {
    const record = parseJsonRecord(candidate);
    if (!record) continue;
    const content = normalizeClarifyInteraction(record);
    if (content.question || content.choices.length) return content;
  }
  return null;
}

export function mergeClarifyInteractionContent(
  payload: Record<string, unknown>,
  content: ClarifyInteractionContent | null,
): Record<string, unknown> {
  if (!content) return payload;
  return {
    ...payload,
    ...(content.question ? { question: content.question } : {}),
    ...(content.choices.length ? { choices: content.choices } : {}),
    ...(content.multiSelect ? { multi_select: true } : {}),
  };
}

/**
 * Batch clarify detection on a server-request payload: `questions` (with `qid`s) is the
 * wire shape `tui_gateway/server.py::_clarify_block` sends. A response with neither
 * `answer` nor `answers` is a cancel-all, so single questions answer with `answer`.
 */
export function isBatchClarifyRequest(payload: Record<string, unknown>): boolean {
  return Array.isArray(payload.questions)
    && (payload.questions as unknown[]).some((q) => typeof (q as Record<string, unknown>)?.qid === 'string'
      && String((q as Record<string, unknown>).qid).trim().length > 0);
}

/**
 * Build the batch `answers` result: the locked answers from the reconnect replay
 * (`params.answers`) merged with the answer the user just submitted. A single answer
 * applies to the first unanswered question (the drawer answers one question at a time).
 */
export function buildClarifyAnswers(payload: Record<string, unknown>, answer: string): Record<string, string> {
  const answers: Record<string, string> = {};
  const locked = payload.answers;
  if (locked && typeof locked === 'object' && !Array.isArray(locked)) {
    for (const [qid, value] of Object.entries(locked as Record<string, unknown>)) {
      if (typeof value === 'string' && qid.trim()) answers[qid] = value;
    }
  }
  const questions = Array.isArray(payload.questions) ? (payload.questions as unknown[]) : [];
  const target = questions.find((q) => {
    const qid = typeof (q as Record<string, unknown>)?.qid === 'string' ? String((q as Record<string, unknown>).qid).trim() : '';
    return qid && !(qid in answers);
  });
  const targetQid = typeof (target as Record<string, unknown> | undefined)?.qid === 'string'
    ? String((target as Record<string, unknown>).qid).trim()
    : '';
  if (targetQid && answer.trim()) answers[targetQid] = answer;
  return answers;
}

export function interactionTitle(interaction: GatewayInteractionRequest): string {
  if (interaction.kind === 'approval') return 'Hermes needs permission';
  if (interaction.kind === 'clarify') return 'Hermes needs your answer';
  if (interaction.kind === 'sudo') return 'Hermes needs elevated access';
  if (interaction.kind === 'terminal_read') return 'Hermes needs terminal output';
  return 'Hermes needs a secret';
}
