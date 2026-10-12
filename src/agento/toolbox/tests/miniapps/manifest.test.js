import { describe, it, expect } from 'vitest';
import { parseManifest } from '../../../modules/miniapps/toolbox/manifest.js';

const manifest = (over = {}) => Buffer.from(JSON.stringify({ schema: 1, title: 'Demo', actions: ['jira_get_issue', 'jira_search'], ...over }));

describe('manifest', () => {
  it('accepts the strict shape', () => {
    expect(parseManifest(manifest())).toEqual({ title: 'Demo', actions: ['jira_get_issue', 'jira_search'] });
    expect(parseManifest(manifest({ actions: [] }))).toEqual({ title: 'Demo', actions: [] });
  });
  it.each([
    ['an unknown key', { extra: 1 }], ['another schema', { schema: 2 }], ['an empty title', { title: '' }],
    ['a long title', { title: 'x'.repeat(201) }], ['a duplicate action', { actions: ['a', 'a'] }],
    ['a bad action name', { actions: ['Bad-Name'] }], ['too many actions', { actions: Array.from({ length: 65 }, (_, i) => `t${i}`) }],
    ['actions not a list', { actions: 'a' }],
  ])('refuses %s', (_n, over) => expect(parseManifest(manifest(over))).toBeNull());
  it('refuses bytes that are not valid UTF-8', () => {
    const bad = Buffer.concat([Buffer.from('{"schema":1,"title":"'), Buffer.from([0xff]), Buffer.from('","actions":[]}')]);
    expect(parseManifest(bad)).toBeNull();
  });
  it('refuses a missing key, not JSON and an array', () => {
    expect(parseManifest(Buffer.from('{"schema":1,"title":"x"}'))).toBeNull();
    expect(parseManifest(Buffer.from('nope'))).toBeNull();
    expect(parseManifest(Buffer.from('[]'))).toBeNull();
  });
});
