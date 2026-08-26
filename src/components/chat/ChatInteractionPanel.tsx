import { useI18n } from '../../lib/i18n';
import { Check, KeyRound, ShieldCheck } from 'lucide-react';
import { interactionTitle } from '../../lib/chat-interactions';
import type { GatewayInteractionRequest } from '../../lib/chat-protocol';

type ChatInteractionPanelProps = {
  interaction: GatewayInteractionRequest;
  choices: string[];
  multiSelect: boolean;
  selectedChoices: string[];
  interactionDraft: string;
  onDraftChange: (value: string) => void;
  onChoice: (choice: string) => void;
  onSubmit: (answer: string, choice?: string, resolveAll?: boolean) => void;
  approvalCommand: string;
  approvalDescription: string;
  interactionQuestion: string;
  interactionPrompt: string;
  secretEnvVar: string;
};

export function ChatInteractionPanel({
  interaction,
  choices,
  multiSelect,
  selectedChoices,
  interactionDraft,
  onDraftChange,
  onChoice,
  onSubmit,
  approvalCommand,
  approvalDescription,
  interactionQuestion,
  interactionPrompt,
  secretEnvVar,
}: ChatInteractionPanelProps) {
  const { t } = useI18n();
  return (
    <section className={`chat-interaction chat-interaction-${interaction.kind}`} aria-label={interactionTitle(interaction)}>
      <div className="chat-interaction-heading">
        <span className="chat-interaction-icon">
          {interaction.kind === 'approval' ? <ShieldCheck size={16} /> : <KeyRound size={16} />}
        </span>
        <div>
          <strong>{interactionTitle(interaction)}</strong>
          <span>{t('interaction.unblocks')}</span>
        </div>
      </div>
      {interaction.kind === 'approval' ? (
        <>
          {approvalDescription ? <p className="chat-interaction-copy">{approvalDescription}</p> : null}
          {approvalCommand ? <code className="chat-command-preview">{approvalCommand}</code> : null}
          <div className="chat-choice-row">
            {(choices.length ? choices : ['once', 'deny']).map((choice) => (
              <button key={choice} type="button" className={`chat-choice ${choice === 'deny' ? 'is-danger' : ''}`} onClick={() => onSubmit(choice, choice, choice === 'always')}>
                {choice === 'deny' ? 'Deny' : choice === 'always' ? 'Always allow' : choice === 'session' ? 'This session' : 'Allow once'}
              </button>
            ))}
          </div>
        </>
      ) : interaction.kind === 'clarify' ? (
        <>
          <p className="chat-interaction-copy">{interactionQuestion || 'Hermes is asking for a decision.'}</p>
          {choices.length ? (
            <div className="chat-choice-row">
              {choices.map((choice) => {
                const selected = selectedChoices.includes(choice);
                return (
                  <button
                    key={choice}
                    type="button"
                    className={`chat-choice ${selected ? 'is-selected' : ''}`}
                    onClick={() => onChoice(choice)}
                  >
                    {selected ? <Check size={14} /> : null}{choice}
                  </button>
                );
              })}
            </div>
          ) : null}
          <div className="chat-interaction-input-row">
            <input value={interactionDraft} onChange={(event) => onDraftChange(event.target.value)} placeholder={t('interaction.typeAnswer')} aria-label={t('interaction.answerHermes')} />
            <button type="button" className="chat-choice is-primary" disabled={!interactionDraft.trim() && (!multiSelect || selectedChoices.length === 0)} onClick={() => onSubmit(interactionDraft.trim() || selectedChoices.join(', '))}>{t('kanban.send')}</button>
          </div>
        </>
      ) : interaction.kind === 'terminal_read' ? (
        <>
          <p className="chat-interaction-copy">{interactionPrompt || 'Paste the requested terminal output.'}</p>
          <div className="chat-interaction-input-row">
            <textarea value={interactionDraft} onChange={(event) => onDraftChange(event.target.value)} placeholder={t('interaction.pasteOutputPlaceholder')} aria-label={t('interaction.terminalOutputAria')} rows={3} />
            <button type="button" className="chat-choice is-primary" disabled={!interactionDraft.trim()} onClick={() => onSubmit(interactionDraft.trim())}>{t('kanban.send')}</button>
          </div>
        </>
      ) : (
        <>
          {interaction.kind === 'secret' ? (
            <p className="chat-interaction-copy">
              {interactionPrompt || 'Hermes needs a secret to continue.'}
              {secretEnvVar ? <><br /><code>{secretEnvVar}</code></> : null}
            </p>
          ) : null}
          <div className="chat-interaction-input-row">
            <input type="password" value={interactionDraft} onChange={(event) => onDraftChange(event.target.value)} placeholder={interaction.kind === 'sudo' ? 'Password' : secretEnvVar || 'Secret value'} aria-label={interaction.kind === 'sudo' ? 'Sudo password' : interactionPrompt || 'Secret value'} autoComplete="off" />
            <button type="button" className="chat-choice is-primary" disabled={!interactionDraft} onClick={() => onSubmit(interactionDraft)}>{t('kanban.send')}</button>
          </div>
        </>
      )}
    </section>
  );
}
