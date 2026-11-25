/** Runs `read` with a signal that aborts after `ms`: the request is cancelled, so its answer
 *  can no longer arrive. The one deadline helper of the panel. */
export async function within<T>(ms: number, read: (signal: AbortSignal) => Promise<T>): Promise<T> {
  const deadline = new AbortController();
  const timer = setTimeout(() => deadline.abort(), ms);
  try {
    return await read(deadline.signal);
  } finally {
    clearTimeout(timer);
  }
}
