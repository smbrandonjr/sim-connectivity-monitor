import { writable } from "svelte/store";

export interface ConfirmRequest {
  title: string;
  message: string;
  confirmLabel: string;
  cancelLabel: string;
  danger: boolean;
  resolve: (ok: boolean) => void;
}

export const confirmStore = writable<ConfirmRequest | null>(null);

/** Themed replacement for window.confirm(). Returns a Promise<boolean>. */
export function confirmDialog(opts: {
  message: string;
  title?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
}): Promise<boolean> {
  return new Promise((resolve) => {
    confirmStore.set({
      title: opts.title ?? "Please confirm",
      message: opts.message,
      confirmLabel: opts.confirmLabel ?? "Confirm",
      cancelLabel: opts.cancelLabel ?? "Cancel",
      danger: opts.danger ?? false,
      resolve,
    });
  });
}
