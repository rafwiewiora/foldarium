# BinaryCIF prototype — 2026-09-01

## Scope

This prototype evaluates BinaryCIF without changing any published Weekly round
or active viewer path. It uses the first target (`11HZ`) from production round
`weekly-2026-08-29-beta-v2` and the deployed Mol* version, 4.6.0.

The representative content-addressed PDB assets were:

- receptor protein: 6,120 atoms;
- pocket: 428 atoms; and
- predicted ligand pose: 35 atoms.

Gemmi 0.7.5 converted PDB to mmCIF. The conversion explicitly emitted
`_chem_comp.type` values before Mol* 4.6.0 performed lossless `cif2bcif`
encoding. Omitting chemical-component typing preserved coordinates but caused
Mol* to render the protein as an untyped atomic assembly rather than a polymer
cartoon, so that metadata is a mandatory scientific and visual guard.

## Results

Each parse result is the median of 50 in-process Mol* parser runs. Download
results are the median of seven fresh requests under a controlled 250 ms RTT,
5 Mbps browser profile.

| Asset | PDB bytes | BinaryCIF bytes | Byte change | PDB download | BinaryCIF download | PDB parse | BinaryCIF parse |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Protein | 483,484 | 63,558 | −86.9% | 1,029.0 ms | 357.7 ms | 1.675 ms | 0.426 ms |
| Pocket | 33,816 | 14,282 | −57.8% | 309.5 ms | 278.3 ms | 0.148 ms | 0.276 ms |
| Pose | 2,769 | 9,506 | +243.3% | 259.1 ms | 271.1 ms | 0.044 ms | 0.241 ms |

The BinaryCIF round trip retained:

- identical atom counts (6,120 / 428 / 35);
- atom, chain, residue, component, alternate-location, and element identity; and
- a maximum coordinate delta of 0.0 Å.

Side-by-side Mol* 4.6.0 rendering showed matching protein cartoons only after
the explicit chemical-component typing was present. Both viewers reported
6,120 structure elements.

## Decision

BinaryCIF is justified for large receptor proteins: this fixture reduced bytes
by 86.9%, controlled download time by 65.2%, and parser time by 74.5%.

Do not encode small ligand poses as BinaryCIF: container/schema overhead made
this fixture 3.4× the PDB size and more than 5× slower to parse. Pocket BinaryCIF
reduced bytes but did not improve parsing and offers little end-to-end benefit.
Keeping pockets and poses as PDB also preserves the current H-bond path, which
concatenates their text records.

## Future-round contract

`binarycif-assets.js` prototypes a fail-closed dual-format descriptor:

- PDB remains mandatory and content-addressed;
- BinaryCIF is optional, independently content-addressed, and bound to the
  source PDB SHA-256;
- Mol* loads BinaryCIF with `isBinary: true` and format `mmcif`; and
- any text-dependent operation forces PDB fallback.

For a future round, publish dual-format receptor metadata only after the
pipeline validates chemical-component typing, identity, coordinates, and
rendered representation. Existing manifests and their hashes must remain
unchanged. The prototype is intentionally not imported by the production app.
