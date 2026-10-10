/** A non-2xx answer. `message` is the API's `error` text, never a stack or a header. */
export class ApiError extends Error {
  readonly status: number;
  readonly retryAfter?: number;
  /** The parsed JSON body, for a caller that relays it as is (the miniapp bridge). */
  readonly body?: unknown;
  constructor(status: number, message: string, retryAfter?: number, body?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.retryAfter = retryAfter;
    this.body = body;
  }
}

/** A write was refused before it was sent: there is no CSRF token (no session). */
export class CsrfMissingError extends Error {
  constructor() { super("not signed in"); this.name = "CsrfMissingError"; }
}

/** The answer came back after the session it was sent under had ended; it is dropped. */
export class SessionChangedError extends Error {
  constructor() { super("the session changed"); this.name = "SessionChangedError"; }
}
