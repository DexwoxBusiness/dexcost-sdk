import { AsyncLocalStorage } from "node:async_hooks";

/** Context-local owner for one logical provider operation. */
const ownerStore = new AsyncLocalStorage<string>();

export function currentProviderCaptureOwner(): string | undefined {
  return ownerStore.getStore();
}

/** Run a provider invocation under the outermost attribution owner. */
export function runWithProviderCapture<T>(owner: string, fn: () => T): T {
  return ownerStore.run(owner, fn);
}

export function providerCaptureIsClaimed(): boolean {
  return currentProviderCaptureOwner() !== undefined;
}
