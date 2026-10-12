import { adminRoute } from '../../versioned_artifacts/toolbox/admin-api.js';
import { ERROR_CODES } from '../../versioned_artifacts/toolbox/errors.js';
import { MANIFEST_FILE, MAX_MANIFEST_BYTES, parseManifest } from './manifest.js';

// The one store read web needs to approve or launch a miniapp (plan §2.4), under the /admin
// guard: a version (or `current`) → its served tree restored, its content id and its manifest.
// A missing or invalid manifest is `null`; web decides what that means.
export async function register(_server, context) {
  const { app } = context;
  if (!app) return;   // MCP sessions: nothing here is a tool
  app.post('/admin/miniapps/:code/resolve', adminRoute(context, async (svc, req) => {
    const code = req.params.code;
    const asked = req.body?.version;
    const versionId = asked === 'current' ? (await svc.getCurrent(code)).current_version : asked;
    const contentId = await svc.versionContentId(code, versionId);
    await svc.ensureServed(code, versionId);
    let bytes = null;
    try {
      bytes = await svc.readVersionFile(code, versionId, MANIFEST_FILE, MAX_MANIFEST_BYTES);
    } catch (err) {
      if (err?.code !== ERROR_CODES.FILE_TOO_LARGE) throw err;
    }
    return { version_id: versionId, content_id: contentId, manifest: bytes && parseManifest(bytes) };
  }));
}
