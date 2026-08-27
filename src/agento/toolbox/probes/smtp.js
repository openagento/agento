// The `smtp` config-test kind. `transporter.verify()` performs exactly the
// handshake the alert path performs — connect, EHLO, STARTTLS, LOGIN — and then
// quits. It never sends a message: testing a credential must not put mail in
// somebody's inbox.
import nodemailer from 'nodemailer';

export const required = ['host'];

const DEFAULT_PORT = 587;

function truthy(v) {
  return v === true || v === 1 || /^(1|true|yes|on)$/i.test(String(v ?? ''));
}

// nodemailer reports the class of failure on `code`; the numeric SMTP reply is
// on `responseCode`. Only these coarse labels cross the boundary — the code is a
// machine-readable label, never prose.
// `fail` is a VERDICT on the credential and only the server can give one. A DNS
// miss, a refused connection or a timeout means the probe never got as far as
// asking — that is `error` ("could not check"), the same distinction the http
// probe already draws in its catch block. Reporting a firewall as "credential
// rejected" is how a working password gets rotated for nothing.
function classify(err) {
  const code = String((err && err.code) || '');
  if (code === 'EAUTH') return { status: 'fail', code: 'AUTH_FAILED' };
  if (code === 'ECONNECTION' || code === 'ETIMEDOUT' || code === 'ESOCKET') {
    return { status: 'error', code: 'CONNECTION_FAILED' };
  }
  if (code === 'EDNS') return { status: 'error', code: 'DNS_FAILED' };
  return { status: 'error', code: 'SMTP_ERROR' };
}

export async function probe(spec, { timeoutMs = 15_000, deps = {} } = {}) {
  const createTransport = deps.createTransport || nodemailer.createTransport;
  const port = parseInt(spec.port ?? DEFAULT_PORT, 10) || DEFAULT_PORT;
  const secure = port === 465;                    // implicit TLS, never STARTTLS
  const user = spec.user ? String(spec.user) : '';

  const transporter = createTransport({
    host: String(spec.host),
    port,
    secure,
    requireTLS: !secure && truthy(spec.starttls),
    auth: user ? { user, pass: String(spec.pass ?? '') } : undefined,
    connectionTimeout: timeoutMs,
    greetingTimeout: timeoutMs,
    socketTimeout: timeoutMs,
  });

  const started = Date.now();
  try {
    await transporter.verify();
    return { status: 'ok', code: 'OK', ms: Date.now() - started };
  } catch (err) {
    const { status, code } = classify(err);
    return {
      status,
      code,
      detail: String((err && err.message) || 'SMTP handshake failed'),
      ms: Date.now() - started,
    };
  } finally {
    // `close()` is optional on an injected stub and on some transports.
    if (typeof transporter.close === 'function') transporter.close();
  }
}
