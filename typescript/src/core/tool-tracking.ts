export type ToolTerminalStatus = "succeeded" | "failed" | "cancelled";

export interface ToolDecoratorHooks<State> {
  begin(): State | undefined;
  run<T>(state: State, action: () => T): T;
  finish(state: State, status: ToolTerminalStatus, durationMs: number, error?: unknown): void;
}

type AnyFunction = (this: unknown, ...args: any[]) => any;

function durationMs(startedAt: number): number {
  return Math.max(0, Math.round(performance.now() - startedAt));
}

function errorStatus(error: unknown): Exclude<ToolTerminalStatus, "succeeded"> {
  const name = error instanceof Error ? error.name.toLowerCase() : "";
  return name.includes("cancel") || name.includes("abort") ? "cancelled" : "failed";
}

function safeBegin<State>(hooks: ToolDecoratorHooks<State>): State | undefined {
  try { return hooks.begin(); } catch { return undefined; }
}

function safeRun<State, T>(hooks: ToolDecoratorHooks<State>, state: State | undefined, action: () => T): T {
  if (state === undefined) return action();
  try { return hooks.run(state, action); } catch (error) {
    // A user exception and an instrumentation exception are indistinguishable
    // here; hooks.run must be a transparent context boundary. Re-throw it.
    throw error;
  }
}

function safeFinish<State>(
  hooks: ToolDecoratorHooks<State>,
  state: State | undefined,
  status: ToolTerminalStatus,
  startedAt: number,
  error?: unknown,
): void {
  if (state === undefined) return;
  try { hooks.finish(state, status, durationMs(startedAt), error); } catch { /* fail open */ }
}

function proxyPromise<State>(
  raw: Record<PropertyKey, unknown>,
  hooks: ToolDecoratorHooks<State>,
  state: State | undefined,
  startedAt: number,
): unknown {
  let finished = false;
  const complete = (status: ToolTerminalStatus, error?: unknown): void => {
    if (finished) return;
    finished = true;
    safeFinish(hooks, state, status, startedAt, error);
  };
  return new Proxy(raw, {
    get(target, property, receiver): unknown {
      if (property === "then") {
        return (onFulfilled?: (value: unknown) => unknown, onRejected?: (error: unknown) => unknown) =>
          (target.then as (...values: unknown[]) => unknown).call(
            target,
            (value: unknown) => safeRun(hooks, state, () => {
              complete("succeeded");
              return onFulfilled === undefined ? value : onFulfilled(value);
            }),
            (error: unknown) => safeRun(hooks, state, () => {
              complete(errorStatus(error), error);
              if (onRejected !== undefined) return onRejected(error);
              throw error;
            }),
          );
      }
      const value = Reflect.get(target, property, receiver);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}

function proxyIterator<State>(
  raw: Record<PropertyKey, unknown>,
  asyncIterator: boolean,
  hooks: ToolDecoratorHooks<State>,
  state: State | undefined,
  startedAt: number,
): unknown {
  const symbol = asyncIterator ? Symbol.asyncIterator : Symbol.iterator;
  let finished = false;
  const complete = (status: ToolTerminalStatus, error?: unknown): void => {
    if (finished) return;
    finished = true;
    safeFinish(hooks, state, status, startedAt, error);
  };
  const iterator = (raw[symbol] as () => any).call(raw);
  const wrapped: any = asyncIterator ? {
    async next(value?: unknown): Promise<IteratorResult<unknown>> {
      try {
        const result = await safeRun(hooks, state, () => iterator.next(value));
        if (result.done) complete("succeeded");
        return result;
      } catch (error) { complete(errorStatus(error), error); throw error; }
    },
    async return(value?: unknown): Promise<IteratorResult<unknown>> {
      try {
        const result = iterator.return === undefined
          ? { done: true, value }
          : await safeRun(hooks, state, () => iterator.return(value));
        complete("cancelled");
        return result;
      } catch (error) { complete(errorStatus(error), error); throw error; }
    },
    async throw(error?: unknown): Promise<IteratorResult<unknown>> {
      try {
        if (iterator.throw === undefined) throw error;
        return await safeRun(hooks, state, () => iterator.throw(error));
      } catch (raised) { complete(errorStatus(raised), raised); throw raised; }
    },
    [Symbol.asyncIterator](): AsyncIterator<unknown> { return this; },
  } : {
    next(value?: unknown): IteratorResult<unknown> {
      try {
        const result = safeRun(hooks, state, () => iterator.next(value));
        if (result.done) complete("succeeded");
        return result;
      } catch (error) { complete(errorStatus(error), error); throw error; }
    },
    return(value?: unknown): IteratorResult<unknown> {
      try {
        const result = iterator.return === undefined
          ? { done: true, value }
          : safeRun(hooks, state, () => iterator.return(value));
        complete("cancelled");
        return result;
      } catch (error) { complete(errorStatus(error), error); throw error; }
    },
    throw(error?: unknown): IteratorResult<unknown> {
      try {
        if (iterator.throw === undefined) throw error;
        return safeRun(hooks, state, () => iterator.throw(error));
      } catch (raised) { complete(errorStatus(raised), raised); throw raised; }
    },
    [Symbol.iterator](): Iterator<unknown> { return this; },
  };
  return new Proxy(raw, {
    get(target, property, receiver): unknown {
      if (property === symbol) return () => wrapped;
      if (property === "next" || property === "return" || property === "throw") {
        return wrapped[property].bind(wrapped);
      }
      const value = Reflect.get(target, property, receiver);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}

/** Preserve sync, Promise, generator, and async-generator tool lifecycles. */
export function decorateTool<State, F extends AnyFunction>(
  fn: F,
  hooks: ToolDecoratorHooks<State>,
): F {
  const generatorKind = fn.constructor?.name;
  return function (this: unknown, ...args: Parameters<F>): ReturnType<F> {
    const state = safeBegin(hooks);
    const startedAt = performance.now();
    let result: unknown;
    try {
      result = safeRun(hooks, state, () => fn.apply(this, args));
    } catch (error) {
      safeFinish(hooks, state, errorStatus(error), startedAt, error);
      throw error;
    }
    const candidate = result as Record<PropertyKey, unknown> | null;
    if (generatorKind === "AsyncGeneratorFunction" && candidate !== null) {
      return proxyIterator(candidate, true, hooks, state, startedAt) as ReturnType<F>;
    }
    if (generatorKind === "GeneratorFunction" && candidate !== null) {
      return proxyIterator(candidate, false, hooks, state, startedAt) as ReturnType<F>;
    }
    if (candidate !== null && candidate !== undefined && typeof candidate.then === "function") {
      return proxyPromise(candidate, hooks, state, startedAt) as ReturnType<F>;
    }
    safeFinish(hooks, state, "succeeded", startedAt);
    return result as ReturnType<F>;
  } as F;
}
