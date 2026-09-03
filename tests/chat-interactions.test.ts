import {
  extractClarifyToolContent,
  mergeClarifyInteractionContent,
  normalizeClarifyInteraction,
} from '../src/lib/chat-interactions.ts';

function assertEqual<T>(actual: T, expected: T) {
  if (actual !== expected) throw new Error(`Expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
}

function assertDeepEqual(actual: unknown, expected: unknown) {
  const actualJson = JSON.stringify(actual);
  const expectedJson = JSON.stringify(expected);
  if (actualJson !== expectedJson) throw new Error(`Expected ${expectedJson}, got ${actualJson}`);
}

const structuredClarify = {
  responses: [{
    question: 'Quale model/provider vuoi assegnare alla Kanban lane?',
    choices_offered: ['OpenRouter', 'oMLX locale', 'Router automatico'],
    user_response: '',
  }],
};

assertDeepEqual(normalizeClarifyInteraction(structuredClarify), {
  question: 'Quale model/provider vuoi assegnare alla Kanban lane?',
  choices: ['OpenRouter', 'oMLX locale', 'Router automatico'],
  multiSelect: false,
  questionId: null,
});

const toolContent = extractClarifyToolContent({
  type: 'tool.started',
  payload: {
    tool_name: 'clarify',
    args: {
      question: 'Quale model/provider vuoi assegnare alla Kanban lane?',
      choices: ['OpenRouter', 'oMLX locale', 'Router automatico'],
    },
  },
});
assertDeepEqual(toolContent, {
  question: 'Quale model/provider vuoi assegnare alla Kanban lane?',
  choices: ['OpenRouter', 'oMLX locale', 'Router automatico'],
  multiSelect: false,
  questionId: null,
});

assertDeepEqual(mergeClarifyInteractionContent({ request_id: 'clarify-1' }, toolContent), {
  request_id: 'clarify-1',
  question: 'Quale model/provider vuoi assegnare alla Kanban lane?',
  choices: ['OpenRouter', 'oMLX locale', 'Router automatico'],
});

assertDeepEqual(normalizeClarifyInteraction({
  questions: [{ qid: 'planet-1', question: 'Quale ambiente?', choices: ['Locale', 'Cloud'], multi_select: true }],
}), {
  question: 'Quale ambiente?',
  choices: ['Locale', 'Cloud'],
  multiSelect: true,
  questionId: 'planet-1',
});

assertEqual(extractClarifyToolContent({ type: 'tool.completed', payload: { tool_name: 'terminal', result: '{}' } }), null);

console.log('chat interaction tests passed');
