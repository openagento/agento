import { describe, expect, it } from 'vitest';
import { redactSecrets } from '../config-loader.js';

describe('redactSecrets', () => {
  it('masks a secret that an upstream error echoed back', () => {
    // The shape of the real incident: a 535 from the mail provider, with the credential
    // quoted back by the server.
    const msg = "535 5.7.8 authentication failed for user with pass hunter2xyz";
    expect(redactSecrets(msg, ['hunter2xyz'])).toBe(
      '535 5.7.8 authentication failed for user with pass ***'
    );
  });

  it('masks the longer secret first when one contains the other', () => {
    expect(redactSecrets('ab abcd', ['ab', 'abcd'])).toBe('*** ***');
  });

  it('leaves a message with no secret in it untouched', () => {
    expect(redactSecrets('535 authentication failed', ['hunter2'])).toBe(
      '535 authentication failed'
    );
  });

  it('is a no-op for a non-string or an empty secret list', () => {
    expect(redactSecrets(undefined, ['x'])).toBe(undefined);
    expect(redactSecrets('text', [])).toBe('text');
    expect(redactSecrets('text', [''])).toBe('text');
  });
});
