# Runs & Poses crystal-aligned preview

This is a derived, 38-system Foldarium/Portal preview of the Runs & Poses
prediction archive. Source predictions use independent model coordinate frames;
these files make the selected systems directly overlayable with experiment.

The experimental system is fixed. Every complete prediction is moved with one
OpenStructure 2.8-compatible, 4 Å binding-site-derived rigid transform; ligand
coordinates are never fitted or moved independently. In each `alignment.json`,
the convention is:

`aligned_xyz = rotation @ raw_xyz + translation`

The manifest also records the exact raw archive member and SHA-256, official R&P
RMSD ligand chain, receptor chain mapping, transform, reported scorer RMSD, and
post-write validation result. The RMSD is recalculated from the emitted browser
PDB with chemical symmetry correction and must agree with R&P within 0.005 Å.
Legacy heavy-atom-count ligand matches and any corrected/scored equivalent copy
remain explicit in the provenance. Complete aligned model CIFs were omitted
from this lightweight browser build.

`collection.json` indexes all systems. A build is published only when every
selected pose validates; this export contains 38 systems and 452 poses.
