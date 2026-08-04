# Runs & Poses crystal-aligned preview

This is a derived, 38-system Foldarium/Portal preview of the Runs & Poses
prediction archive. Source predictions use independent model coordinate frames;
these files make the selected systems directly overlayable with experiment.

The experimental system is fixed. Every complete prediction is moved with one
receptor-derived rigid transform; ligand coordinates are never fitted or moved
independently. In each `alignment.json`, the convention is:

`aligned_xyz = rotation @ raw_xyz + translation`

The manifest also records the exact raw archive member and SHA-256, receptor
chain mapping, sequence/fit diagnostics, published scorer RMSD, and validation
result. Chemically identical ligand copies are scorer-equivalent and retain both
their indexed and resolved chain identities. Complete aligned model CIFs were omitted from this lightweight browser build.

`collection.json` indexes all systems. A build is published only when every
selected pose validates; this export contains 38 systems and 452 poses.
