import path from 'node:path';
import { mkdir, readdir, readlink, rename, rm, stat, symlink } from 'node:fs/promises';
import { randomBytes } from 'node:crypto';
import { VERSION_ID_RE, validateArtifactCode, validateVersionId } from './paths.js';

// Plain filesystem, no Git and no service: this is the tree an HTTP server reads, and
// keeping it free of the storage engine is what lets the serving container stay a
// static file server. `service.js` is its only importer.

const artifactDir = (publishedRoot, code) => path.join(publishedRoot, validateArtifactCode(code));

/** Only these two errnos mean "not there". Every other one means the answer is UNKNOWN,
 *  and reporting unknown as absent is what lets retention delete the directory the
 *  server is serving. An `EINVAL` from `readlink` — `current` exists but is not a
 *  symlink — is exactly such an unknown, so it propagates too. */
const isMissing = (err) => err?.code === 'ENOENT' || err?.code === 'ENOTDIR';

/** The layout is chosen so the URL path maps 1:1 onto it. */
export const publishedVersionDir = (publishedRoot, code, versionId) =>
  path.join(artifactDir(publishedRoot, code), 'v', validateVersionId(versionId));

export const currentLink = (publishedRoot, code) => path.join(artifactDir(publishedRoot, code), 'current');

/** Under the PUBLISHED root, not the store's: `published_root` is a separate config
 *  path and need not share a filesystem with `storage_root`, and `rename()` across
 *  devices fails EXDEV. The leading dot keeps it out of every listing for the same
 *  reason the store's does — `ARTIFACT_CODE_RE` requires a leading [a-z0-9], so no
 *  artifact can ever be named `.tmp`. */
export const scratchRoot = (publishedRoot) => path.join(publishedRoot, '.tmp');

/** Boot-time only: a materialization killed between its extraction and its `finally`
 *  leaves a scratch tree behind, and `finally` does not run on SIGKILL. */
export const sweepScratch = (publishedRoot) => rm(scratchRoot(publishedRoot), { recursive: true, force: true });

/** `symlink` + `rename`, never `mv`: a measured `mv` swap left `current` on the old
 *  version and dropped a stray link INSIDE it. The target is RELATIVE so the link is
 *  correct whatever absolute path the serving container mounts the root at. */
export async function swapCurrent(publishedRoot, code, versionId) {
  const link = currentLink(publishedRoot, code);
  const tmp = `${link}.tmp-${randomBytes(6).toString('hex')}`;
  await mkdir(path.dirname(link), { recursive: true });
  await symlink(path.join('v', validateVersionId(versionId)), tmp);
  try {
    await rename(tmp, link);
  } catch (err) {
    // No half-swapped state and no stray link left for the next reader to trip on.
    await rm(tmp, { force: true });
    throw err;
  }
}

/** What `current` ACTUALLY points at, read from the link — never from what a caller
 *  believed it had just installed. */
export async function resolveCurrentTarget(publishedRoot, code) {
  let target;
  try { target = await readlink(currentLink(publishedRoot, code)); }
  catch (err) { if (isMissing(err)) return null; throw err; }
  const id = path.basename(target);
  return path.dirname(target) === 'v' && VERSION_ID_RE.test(id) ? id : null;
}

/** `keep === 0` prunes nothing — a deployment that sets it to zero keeps every preview.
 *
 *  The exclusion is read from `current` here rather than taken as a parameter. After a
 *  FAILED swap `current` still points at the OLD version, so excluding the version the
 *  caller intended to install would delete the directory HTTP is serving right now.
 *  The prune list comes from the published tree only. */
export async function pruneVersions(publishedRoot, code, keep) {
  if (!keep) return [];
  const dir = path.join(artifactDir(publishedRoot, code), 'v');
  let names;
  try { names = await readdir(dir); } catch (err) { if (isMissing(err)) return []; throw err; }
  // The id embeds its own UTC save timestamp in a fixed-width form, so descending name
  // IS descending save time — the same ordering `listVersions` uses.
  const newest = names.filter((n) => VERSION_ID_RE.test(n)).sort().reverse();
  const current = await resolveCurrentTarget(publishedRoot, code);
  const doomed = newest.slice(keep).filter((n) => n !== current);
  for (const n of doomed) await rm(path.join(dir, n), { recursive: true, force: true });
  return doomed;
}

/** Everything served for one artifact — the version directories and the `current` link.
 *  Reports whether it was there, so a caller can tell a removal from a no-op and repair
 *  a store that lost one root but not the other. */
export async function removeArtifact(publishedRoot, code) {
  const dir = artifactDir(publishedRoot, code);
  try { await stat(dir); } catch (err) { if (isMissing(err)) return false; throw err; }
  await rm(dir, { recursive: true, force: true });
  return true;
}

/** Relative, and `null` once retention has pruned the directory. A pruned version stays
 *  fully readable through `materialize`; only the browser preview is gone. */
export async function previewPath(publishedRoot, code, versionId) {
  try { await stat(publishedVersionDir(publishedRoot, code, versionId)); }
  catch (err) { if (isMissing(err)) return null; throw err; }
  return `/${code}/v/${versionId}/`;
}

/** The ONE place an absolute URL is built. A wrong absolute URL in a message to a human
 *  is worse than no URL, so the version listing stays relative. */
export const previewUrl = (baseUrl, code) =>
  `${String(baseUrl).replace(/\/+$/, '')}/${validateArtifactCode(code)}/`;
