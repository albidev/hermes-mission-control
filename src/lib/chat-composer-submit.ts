// Deferred-submit orchestration for the Mission Control chat composer.
//
// The optimistic user bubble is appended by the gateway chat layer BEFORE the
// round trip resolves, so the composer draft must be cleared optimistically
// before the first await — otherwise the composer keeps showing text that has
// already been accepted. If the submit is rejected (or an attachment cannot be
// read), the rejected text is handed back to the composer, but only when the
// user has not typed a fresh draft and the session has not changed meanwhile.

export type ComposerAttachmentLike = {
  id: string;
  previewUrl?: string | null;
};

export type ComposerSubmitOutcome = {
  sent: boolean;
  restoredDraft: boolean;
};

export type RunComposerSubmitDeps<TAttachment extends ComposerAttachmentLike, TUpload = TAttachment> = {
  /** The text the user submitted. */
  onError?: (error: unknown) => void;
  text: string;
  /** The session the submit was issued against. */
  sessionId: string | null;
  /** Attachments staged in the composer at submit time. */
  attachments: TAttachment[];
  /** Reads the attachment payloads; may throw when a file cannot be read. */
  readUploads: (attachments: TAttachment[]) => Promise<TUpload[]>;
  /** Submits the prompt; resolves false when the gateway rejected it. */
  submit: (text: string, uploads: TUpload[]) => Promise<boolean>;
  /** Live composer draft (not the captured one) — used to detect a fresh keystroke. */
  readDraft: () => string;
  /** Live session id — used to detect a session switch while in flight. */
  readSessionId: () => string | null;
  /** Writes the composer draft. */
  setDraft: (value: string) => void;
  /** Clears the staged attachments (success path only). */
  clearComposerAttachments: (attachments: TAttachment[]) => void;
};

/**
 * Decide whether a rejected submit may restore its text into the composer.
 *
 * - A non-empty text is required; a whitespace-only prompt is not recoverable.
 * - A draft the user typed while the submit was in flight wins: never clobber it.
 * - A session switch while in flight wins: never restore into another chat.
 */
export function shouldRestoreRejectedDraft(input: {
  text: string;
  sessionId: string | null;
  currentDraft: string;
  currentSessionId: string | null;
}): boolean {
  if (!input.text.trim()) return false;
  if (input.currentDraft !== '') return false;
  if (input.sessionId !== input.currentSessionId) return false;
  return true;
}

export async function runComposerSubmit<TAttachment extends ComposerAttachmentLike, TUpload = TAttachment>(
  deps: RunComposerSubmitDeps<TAttachment, TUpload>,
): Promise<ComposerSubmitOutcome> {
  const {
    text,
    sessionId,
    attachments,
    readUploads,
    submit,
    readDraft,
    readSessionId,
    setDraft,
    clearComposerAttachments,
  } = deps;

  // Optimistic clear BEFORE any await: the user bubble already exists, so the
  // composer must not keep displaying text that has already been accepted.
  setDraft('');

  const restore = (): boolean => {
    const restored = shouldRestoreRejectedDraft({
      text,
      sessionId,
      currentDraft: readDraft(),
      currentSessionId: readSessionId(),
    });
    if (restored) setDraft(text);
    return restored;
  };

  let uploads: TUpload[];
  try {
    uploads = await readUploads(attachments);
  } catch (error) {
    deps.onError?.(error);
    return { sent: false, restoredDraft: restore() };
  }
  try {
    const sent = await submit(text, uploads);
    if (sent) {
      clearComposerAttachments(attachments);
      return { sent: true, restoredDraft: false };
    }
    return { sent: false, restoredDraft: restore() };
  } catch (error) {
    deps.onError?.(error);
    return { sent: false, restoredDraft: restore() };
  }
}
