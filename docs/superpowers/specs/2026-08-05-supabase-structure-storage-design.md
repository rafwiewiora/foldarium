# Supabase Structure Storage Design

## Goal

Serve Foldarium PDB structures from Supabase Storage instead of Vercel while retaining the files in Git as a backup.

## Storage Layout

Use one public Supabase Storage bucket named `structures`. Preserve each repository-relative path as its object key:

- `data/13NM/pose-1.pdb`
- `data/13NM/protein.pdb`
- `data_rnp/<ensemble>/pose-4.pdb`

Stable public object URLs allow existing quiz manifests and future Mol* replay snapshots to refer to the same structures.

## Upload

Add a dependency-free Node script that:

1. Reads `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` from the local environment.
2. Recursively discovers `.pdb` files under `data/` and `data_rnp/`.
3. Creates the public `structures` bucket when it does not exist.
4. Uploads files with bounded concurrency and their repository-relative paths.
5. Skips objects that already exist unless `--overwrite` is supplied.
6. Prints uploaded, skipped, and failed counts and exits nonzero on any failure.

The server credential is never stored in browser code, configuration files, or Git.

## Runtime Resolution

Add `structureBaseUrl` to `supabase-config.js`, pointing to:

`https://<project>.supabase.co/storage/v1/object/public/structures`

Add one `assetUrl(path)` helper:

- absolute HTTP(S) URLs pass through unchanged;
- relative paths are prefixed with `structureBaseUrl` when configured;
- relative paths remain local when the setting is empty.

Use this helper for Mol* structure downloads and raw PDB fetches used by the H-bond overlay. Existing `quiz_items*.json` manifests remain unchanged.

## Vercel

Add `data/` and `data_rnp/` to `.vercelignore`. The files remain in Git but are omitted from future Vercel deployments. Production therefore depends on the public Storage bucket, while local development can still use repository files by clearing `structureBaseUrl`.

## Verification

- Unit-test absolute, Storage-backed, and local path resolution.
- Run the existing test suite.
- Compare discovered local file count with upload results.
- Fetch representative CAMEO and Runs-n-Poses protein, pocket, and pose objects from the public URLs.
- Run the local quiz against Storage and verify question loading and H-bond rendering.

## Failure Behavior

- Missing local upload credentials stop the upload script with a clear message.
- Failed uploads are reported individually and cause a nonzero exit.
- Runtime structure fetch failures surface through the existing quiz error behavior.
- The script does not delete local or remote objects.

## Out of Scope

- Removing PDB files from Git history
- Private buckets or signed URLs
- Database metadata for individual objects
- Full remote checksum verification
- Automatic deletion or synchronization of obsolete remote objects
