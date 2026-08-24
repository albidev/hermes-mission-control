import { type ClipboardEvent, type ChangeEvent, type DragEvent, useCallback, useEffect, useRef, useState } from 'react';
import {
  MAX_ATTACHMENT_BYTES,
  MAX_ATTACHMENTS,
  classifyAttachment,
  type PendingAttachment,
} from '../lib/chat-gateway';

type UseChatAttachmentsResult = {
  pendingAttachments: PendingAttachment[];
  attachmentNotice: string | null;
  isDragging: boolean;
  addFiles: (files: File[]) => void;
  removeAttachment: (id: string) => void;
  handleFileInput: (event: ChangeEvent<HTMLInputElement>) => void;
  handleDrop: (event: DragEvent<HTMLElement>) => void;
  handlePaste: (event: ClipboardEvent<HTMLTextAreaElement>) => void;
  setIsDragging: (value: boolean) => void;
  setAttachmentNotice: (value: string | null) => void;
  clearAttachments: () => void;
};

export function useChatAttachments(): UseChatAttachmentsResult {
  const [pendingAttachments, setPendingAttachments] = useState<PendingAttachment[]>([]);
  const [attachmentNotice, setAttachmentNotice] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const pendingRef = useRef<PendingAttachment[]>([]);

  useEffect(() => {
    pendingRef.current = pendingAttachments;
  }, [pendingAttachments]);

  useEffect(() => () => {
    for (const attachment of pendingRef.current) {
      if (attachment.previewUrl) URL.revokeObjectURL(attachment.previewUrl);
    }
  }, []);

  const addFiles = useCallback((files: File[]) => {
    setAttachmentNotice(null);
    const available = Math.max(0, MAX_ATTACHMENTS - pendingRef.current.length);
    if (available === 0) {
      setAttachmentNotice(`You can attach up to ${MAX_ATTACHMENTS} files per message.`);
      return;
    }
    const accepted: PendingAttachment[] = [];
    for (const file of files.slice(0, available)) {
      if (file.size > MAX_ATTACHMENT_BYTES) {
        setAttachmentNotice(`${file.name} is too large. The limit is 50 MB.`);
        continue;
      }
      const kind = classifyAttachment(file.type, file.name);
      accepted.push({
        id: `${file.name}-${file.lastModified}-${Math.random().toString(36).slice(2)}`,
        kind,
        name: file.name,
        size: file.size,
        mimeType: file.type || undefined,
        file,
        previewUrl: kind === 'image' ? URL.createObjectURL(file) : null,
      });
    }
    if (files.length > available) setAttachmentNotice(`Only ${available} more attachment${available === 1 ? '' : 's'} can be added.`);
    if (accepted.length) setPendingAttachments((current) => [...current, ...accepted]);
  }, []);

  const removeAttachment = useCallback((id: string) => {
    setPendingAttachments((current) => {
      const target = current.find((attachment) => attachment.id === id);
      if (target?.previewUrl) URL.revokeObjectURL(target.previewUrl);
      return current.filter((attachment) => attachment.id !== id);
    });
  }, []);

  const handleFileInput = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    addFiles(Array.from(event.target.files ?? []));
    event.target.value = '';
  }, [addFiles]);

  const handleDrop = useCallback((event: DragEvent<HTMLElement>) => {
    event.preventDefault();
    setIsDragging(false);
    addFiles(Array.from(event.dataTransfer.files ?? []));
  }, [addFiles]);

  const handlePaste = useCallback((event: ClipboardEvent<HTMLTextAreaElement>) => {
    const files = Array.from(event.clipboardData.files ?? []);
    if (files.length) {
      event.preventDefault();
      addFiles(files);
    }
  }, [addFiles]);

  const clearAttachments = useCallback(() => {
    for (const attachment of pendingRef.current) {
      if (attachment.previewUrl) URL.revokeObjectURL(attachment.previewUrl);
    }
    pendingRef.current = [];
    setPendingAttachments([]);
    setAttachmentNotice(null);
  }, []);

  return {
    pendingAttachments,
    attachmentNotice,
    isDragging,
    addFiles,
    removeAttachment,
    handleFileInput,
    handleDrop,
    handlePaste,
    setIsDragging,
    setAttachmentNotice,
    clearAttachments,
  };
}
