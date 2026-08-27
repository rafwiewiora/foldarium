"""Deterministic PDB evidence metrics and orthographic PNG rendering."""

from __future__ import annotations

import math
import statistics
import struct
import zlib
from dataclasses import dataclass
from typing import Iterable, Sequence

from .weekly_llm_contract import sha256_hex

MAX_PDB_BYTES = 5_000_000
MAX_PNG_BYTES = 2_000_000
MAX_IMAGE_DIMENSION = 256
MAX_ATOMS = 8_000
MAX_PROTEIN_ATOMS = 100_000
MAX_PAIR_CHECKS = 250_000
MAX_CONTACT_ENTRIES = 32
MAX_EVIDENCE_JSON_BYTES = 200_000

_CLASH_CUTOFF_ANGSTROM = 2.0
_CLOSE_CONTACT_CUTOFF_ANGSTROM = 4.0
_NO_CONTACT_CUTOFF_ANGSTROM = 3.5
_CONTACT_TABLE_CUTOFF_ANGSTROM = 4.0
_PANEL_NAMES = ("xy", "xz", "yz")
_TWO_LETTER_ELEMENTS = frozenset(
    {"HE", "LI", "BE", "NE", "NA", "MG", "AL", "SI", "CL", "AR", "CA", "SC", "TI", "CR", "MN", "FE", "CO", "NI", "CU", "ZN", "GA", "GE", "AS", "SE", "BR", "KR", "RB", "SR", "ZR", "NB", "MO", "TC", "RU", "RH", "PD", "AG", "CD", "IN", "SN", "SB", "TE", "I", "XE", "CS", "BA", "LA", "CE", "PR", "ND", "PM", "SM", "EU", "GD", "TB", "DY", "HO", "ER", "TM", "YB", "LU", "HF", "TA", "W", "RE", "OS", "IR", "PT", "AU", "HG", "TL", "PB", "BI", "PO", "AT", "RN"}
)
_HALOGENS = frozenset({"F", "CL", "BR", "I"})


@dataclass(frozen=True)
class Atom:
    chain_id: str
    res_name: str
    res_seq: int
    insertion_code: str
    atom_name: str
    serial: int
    x: float
    y: float
    z: float
    element: str
    b_factor: float | None


class WeeklyLlmEvidenceError(ValueError):
    """Raised when evidence generation violates deterministic bounds."""


def _slice_field(line: str, start: int, end: int) -> str:
    if len(line) < end:
        return ""
    return line[start:end].strip()


def _strip_leading_digits(atom_name: str) -> str:
    stripped = atom_name.strip()
    while stripped and stripped[0].isdigit():
        stripped = stripped[1:]
    return stripped


def _infer_element(atom_name: str, res_name: str, element_field: str) -> str:
    element = element_field.strip().upper()
    if len(element) >= 2 and element[:2] in _TWO_LETTER_ELEMENTS:
        return element[:2]
    if element:
        return element[0]
    name = _strip_leading_digits(atom_name).strip()
    if len(name) >= 2 and name[0].isalpha() and name[1].isalpha():
        pair = name[0:2].upper()
        if pair in _TWO_LETTER_ELEMENTS:
            return pair
        return name[0].upper()
    if name and name[0].isalpha():
        return name[0].upper()
    return res_name.strip()[0:1].upper() if res_name.strip() else "X"


def _is_hydrogen(atom_name: str, element: str) -> bool:
    if element == "H":
        return True
    name = _strip_leading_digits(atom_name).upper()
    return name.startswith("H") or name.startswith("D")


def _parse_optional_float(text: str) -> float | None:
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def parse_pdb_atoms(
    content: bytes,
    *,
    label: str,
    max_atoms: int = MAX_ATOMS,
) -> list[Atom]:
    if len(content) > MAX_PDB_BYTES:
        raise WeeklyLlmEvidenceError(f"{label} exceeds {MAX_PDB_BYTES} bytes")
    if (
        isinstance(max_atoms, bool)
        or not isinstance(max_atoms, int)
        or not 1 <= max_atoms <= MAX_PROTEIN_ATOMS
    ):
        raise WeeklyLlmEvidenceError(
            f"{label} max_atoms must be between 1 and {MAX_PROTEIN_ATOMS}"
        )
    atoms: list[Atom] = []
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.decode("utf-8", errors="strict")
        if not line.startswith(("ATOM", "HETATM")):
            continue
        if len(line) < 54:
            raise WeeklyLlmEvidenceError(f"{label} line {line_number} is too short for coordinates")
        record = line[0:6].strip()
        if record not in {"ATOM", "HETATM"}:
            continue
        atom_name = _slice_field(line, 12, 16)
        res_name = _slice_field(line, 17, 20)
        chain_id = _slice_field(line, 21, 22) or "_"
        serial_text = _slice_field(line, 6, 11)
        if not serial_text:
            raise WeeklyLlmEvidenceError(f"{label} line {line_number} missing atom serial")
        res_seq_text = _slice_field(line, 22, 26)
        if not res_seq_text:
            raise WeeklyLlmEvidenceError(f"{label} line {line_number} missing residue sequence")
        insertion_code = line[26:27] if len(line) > 26 else ""
        insertion_code = insertion_code.strip()
        try:
            serial = int(serial_text)
            res_seq = int(res_seq_text)
        except ValueError as error:
            raise WeeklyLlmEvidenceError(
                f"{label} line {line_number} has invalid atom serial or residue sequence"
            ) from error
        try:
            x = float(line[30:38])
            y = float(line[38:46])
            z = float(line[46:54])
        except ValueError as error:
            raise WeeklyLlmEvidenceError(
                f"{label} line {line_number} has invalid coordinates"
            ) from error
        for coord in (x, y, z):
            if not math.isfinite(coord) or abs(coord) > 10_000:
                raise WeeklyLlmEvidenceError(
                    f"{label} line {line_number} has non-finite or out-of-range coordinates"
                )
        b_factor = _parse_optional_float(_slice_field(line, 60, 66))
        element = _infer_element(atom_name, res_name, _slice_field(line, 76, 78))
        if _is_hydrogen(atom_name, element):
            continue
        atoms.append(
            Atom(
                chain_id=chain_id,
                res_name=res_name,
                res_seq=res_seq,
                insertion_code=insertion_code,
                atom_name=atom_name,
                serial=serial,
                x=x,
                y=y,
                z=z,
                element=element,
                b_factor=b_factor,
            )
        )
        if len(atoms) > max_atoms:
            raise WeeklyLlmEvidenceError(f"{label} exceeds {max_atoms} heavy atoms")
    atoms.sort(
        key=lambda atom: (
            atom.chain_id,
            atom.res_seq,
            atom.insertion_code,
            atom.res_name,
            atom.serial,
            atom.atom_name,
            atom.x,
            atom.y,
            atom.z,
        )
    )
    return atoms


def centroid(atoms: Sequence[Atom]) -> tuple[float, float, float]:
    if not atoms:
        raise WeeklyLlmEvidenceError("centroid requires at least one atom")
    xs = [atom.x for atom in atoms]
    ys = [atom.y for atom in atoms]
    zs = [atom.z for atom in atoms]
    count = float(len(atoms))
    return (sum(xs) / count, sum(ys) / count, sum(zs) / count)


def _distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def _residue_key(atom: Atom) -> tuple[str, int, str]:
    return atom.chain_id, atom.res_seq, atom.insertion_code


def _residue_id(atom: Atom) -> str:
    if atom.insertion_code:
        return f"{atom.res_seq}{atom.insertion_code}"
    return str(atom.res_seq)


def _finite_summary(values: Sequence[float]) -> dict[str, float] | None:
    finite = [value for value in values if value is not None and math.isfinite(value)]
    if not finite:
        return None
    return {
        "median": round(float(statistics.median(finite)), 3),
        "min": round(min(finite), 3),
        "max": round(max(finite), 3),
    }


def _build_nearest_contacts(
    receptor: Sequence[Atom],
    ligand: Sequence[Atom],
) -> list[dict[str, str | float | int]]:
    pairs: list[tuple[float, Atom, Atom]] = []
    for lig in ligand:
        for rec in receptor:
            distance = _distance((lig.x, lig.y, lig.z), (rec.x, rec.y, rec.z))
            if distance <= _CONTACT_TABLE_CUTOFF_ANGSTROM:
                pairs.append((distance, lig, rec))
    pairs.sort(
        key=lambda row: (
            row[0],
            row[2].chain_id,
            row[2].res_seq,
            row[2].insertion_code,
            row[2].res_name,
            row[2].atom_name,
            row[1].atom_name,
            row[1].serial,
        )
    )
    contacts: list[dict[str, str | float | int]] = []
    for distance, lig, rec in pairs[:MAX_CONTACT_ENTRIES]:
        contacts.append(
            {
                "ligand_atom_name": lig.atom_name.strip(),
                "ligand_element": lig.element,
                "receptor_residue_name": rec.res_name,
                "receptor_chain_id": rec.chain_id,
                "receptor_residue_id": _residue_id(rec),
                "receptor_atom_name": rec.atom_name.strip(),
                "receptor_element": rec.element,
                "distance_angstrom": round(distance, 3),
            }
        )
    return contacts


def _contact_summaries(
    receptor: Sequence[Atom],
    ligand: Sequence[Atom],
) -> dict[str, int | float | dict[str, float] | None]:
    contact_residues: set[tuple[str, int, str]] = set()
    nearest_distances: list[float] = []
    ligand_atoms_with_contact = 0
    for lig in ligand:
        nearest = float("inf")
        has_contact = False
        for rec in receptor:
            distance = _distance((lig.x, lig.y, lig.z), (rec.x, rec.y, rec.z))
            if distance < nearest:
                nearest = distance
            if distance <= _CONTACT_TABLE_CUTOFF_ANGSTROM:
                contact_residues.add(_residue_key(rec))
                has_contact = True
        if has_contact:
            ligand_atoms_with_contact += 1
        if math.isfinite(nearest):
            nearest_distances.append(nearest)
    ligand_contact_fraction = round(ligand_atoms_with_contact / len(ligand), 4) if ligand else 0.0
    pocket_b_factors = [atom.b_factor for atom in receptor if atom.b_factor is not None]
    summary: dict[str, int | float | dict[str, float] | None] = {
        "contact_residue_count": len(contact_residues),
        "ligand_contact_fraction": ligand_contact_fraction,
        "nearest_distance_median_angstrom": round(float(statistics.median(nearest_distances)), 3)
        if nearest_distances
        else None,
        "nearest_distance_max_angstrom": round(max(nearest_distances), 3) if nearest_distances else None,
        "pocket_b_factor": _finite_summary([value for value in pocket_b_factors if value is not None]),
        "pocket_plddt": _finite_summary([value for value in pocket_b_factors if value is not None]),
    }
    return summary


def _pairwise_metrics(receptor: Sequence[Atom], ligand: Sequence[Atom]) -> dict[str, int | float]:
    if not receptor:
        raise WeeklyLlmEvidenceError("receptor/pocket atoms are required for metrics")
    if not ligand:
        raise WeeklyLlmEvidenceError("ligand atoms are required for metrics")
    pair_budget = len(receptor) * len(ligand)
    if pair_budget > MAX_PAIR_CHECKS:
        raise WeeklyLlmEvidenceError(
            f"receptor/ligand pair count {pair_budget} exceeds {MAX_PAIR_CHECKS}"
        )
    min_distance = float("inf")
    clash_count = 0
    close_contact_count = 0
    possible_n_o_contact_count = 0
    for left in receptor:
        for right in ligand:
            distance = _distance((left.x, left.y, left.z), (right.x, right.y, right.z))
            if distance < min_distance:
                min_distance = distance
            if distance < _CLASH_CUTOFF_ANGSTROM:
                clash_count += 1
            if distance < _CLOSE_CONTACT_CUTOFF_ANGSTROM:
                close_contact_count += 1
            if (
                distance < _NO_CONTACT_CUTOFF_ANGSTROM
                and left.element in {"N", "O"}
                and right.element in {"N", "O"}
            ):
                possible_n_o_contact_count += 1
    return {
        "min_receptor_ligand_distance_angstrom": round(min_distance, 3),
        "clash_count": clash_count,
        "close_contact_count": close_contact_count,
        "possible_n_o_contact_count": possible_n_o_contact_count,
    }


def compute_geometry_metrics(
    *,
    pose_atoms: Sequence[Atom],
    protein_atoms: Sequence[Atom],
    pocket_atoms: Sequence[Atom],
) -> dict[str, int | float]:
    receptor = list(pocket_atoms)
    if not receptor:
        raise WeeklyLlmEvidenceError("pocket atoms must be present for bounded metrics")
    if not pose_atoms:
        raise WeeklyLlmEvidenceError("ligand atoms must be present for metrics")
    pose_centroid = centroid(pose_atoms)
    pocket_centroid = centroid(receptor)
    metrics = _pairwise_metrics(receptor, pose_atoms)
    metrics.update(
        {
            "pose_atom_count": len(pose_atoms),
            "protein_atom_count": len(protein_atoms),
            "pocket_atom_count": len(pocket_atoms),
            "centroid_distance_angstrom": round(_distance(pose_centroid, pocket_centroid), 3),
        }
    )
    return metrics


def write_png_rgb(*, width: int, height: int, pixels: Iterable[tuple[int, int, int]]) -> bytes:
    if width <= 0 or height <= 0 or width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        raise WeeklyLlmEvidenceError("PNG dimensions are out of bounds")
    raw_rows = bytearray()
    pixel_iter = iter(pixels)
    for _row in range(height):
        raw_rows.append(0)
        for _col in range(width):
            try:
                red, green, blue = next(pixel_iter)
            except StopIteration as error:
                raise WeeklyLlmEvidenceError("PNG pixel buffer is incomplete") from error
            for channel in (red, green, blue):
                if not isinstance(channel, int) or channel < 0 or channel > 255:
                    raise WeeklyLlmEvidenceError("PNG channel values must be 0-255")
            raw_rows.extend((red, green, blue))
    compressed = zlib.compress(bytes(raw_rows), level=9)

    def chunk(tag: bytes, data: bytes) -> bytes:
        payload = tag + data
        crc = zlib.crc32(payload) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")
    if len(png) > MAX_PNG_BYTES:
        raise WeeklyLlmEvidenceError(f"PNG exceeds {MAX_PNG_BYTES} bytes")
    return png


def _axis_value(atom: Atom, axis: str) -> float:
    if axis == "x":
        return atom.x
    if axis == "y":
        return atom.y
    return atom.z


def _shared_bounds(*groups: Sequence[Atom]) -> tuple[float, float, float, float, float, float]:
    atoms = [atom for group in groups for atom in group]
    if not atoms:
        raise WeeklyLlmEvidenceError("shared bounds require atoms")
    return (
        min(atom.x for atom in atoms),
        max(atom.x for atom in atoms),
        min(atom.y for atom in atoms),
        max(atom.y for atom in atoms),
        min(atom.z for atom in atoms),
        max(atom.z for atom in atoms),
    )


def _project_shared(
    atoms: Sequence[Atom],
    *,
    axis_a: str,
    axis_b: str,
    bounds: tuple[float, float, float, float, float, float],
    width: int,
    height: int,
    margin: int,
) -> list[tuple[int, int, Atom]]:
    min_x, max_x, min_y, max_y, min_z, max_z = bounds
    axis_min = {"x": min_x, "y": min_y, "z": min_z}
    axis_max = {"x": max_x, "y": max_y, "z": max_z}
    span_a = max(axis_max[axis_a] - axis_min[axis_a], 1e-6)
    span_b = max(axis_max[axis_b] - axis_min[axis_b], 1e-6)
    drawable_w = max(width - 2 * margin, 1)
    drawable_h = max(height - 2 * margin, 1)
    points: list[tuple[int, int, Atom]] = []
    for atom in atoms:
        a = _axis_value(atom, axis_a)
        b = _axis_value(atom, axis_b)
        px = margin + int(((a - axis_min[axis_a]) / span_a) * (drawable_w - 1))
        py = margin + int(((b - axis_min[axis_b]) / span_b) * (drawable_h - 1))
        points.append((px, height - 1 - py, atom))
    return points


def _project_point(
    *,
    a: float,
    b: float,
    axis_a: str,
    axis_b: str,
    bounds: tuple[float, float, float, float, float, float],
    width: int,
    height: int,
    margin: int,
) -> tuple[int, int]:
    min_x, max_x, min_y, max_y, min_z, max_z = bounds
    axis_min = {"x": min_x, "y": min_y, "z": min_z}
    axis_max = {"x": max_x, "y": max_y, "z": max_z}
    span_a = max(axis_max[axis_a] - axis_min[axis_a], 1e-6)
    span_b = max(axis_max[axis_b] - axis_min[axis_b], 1e-6)
    drawable_w = max(width - 2 * margin, 1)
    drawable_h = max(height - 2 * margin, 1)
    px = margin + int(((a - axis_min[axis_a]) / span_a) * (drawable_w - 1))
    py = margin + int(((b - axis_min[axis_b]) / span_b) * (drawable_h - 1))
    return px, height - 1 - py


def _ligand_color(atom: Atom) -> tuple[int, int, int]:
    element = atom.element
    if element == "N":
        return (30, 90, 220)
    if element == "O":
        return (220, 50, 50)
    if element == "S":
        return (220, 200, 40)
    if element in _HALOGENS:
        return (40, 180, 80)
    return (180, 80, 80)


def render_panel_pixels(
    *,
    receptor_atoms: Sequence[Atom],
    ligand_atoms: Sequence[Atom],
    axis_a: str,
    axis_b: str,
    bounds: tuple[float, float, float, float, float, float],
    width: int = 84,
    height: int = 96,
    draw_contacts: bool = True,
) -> list[tuple[int, int, int]]:
    pixels = [(255, 255, 255)] * (width * height)
    receptor_points = _project_shared(
        receptor_atoms,
        axis_a=axis_a,
        axis_b=axis_b,
        bounds=bounds,
        width=width,
        height=height,
        margin=6,
    )
    ligand_points = _project_shared(
        ligand_atoms,
        axis_a=axis_a,
        axis_b=axis_b,
        bounds=bounds,
        width=width,
        height=height,
        margin=6,
    )
    if draw_contacts:
        for ligand in ligand_atoms:
            for receptor in receptor_atoms:
                if _distance((ligand.x, ligand.y, ligand.z), (receptor.x, receptor.y, receptor.z)) >= _CLOSE_CONTACT_CUTOFF_ANGSTROM:
                    continue
                mid_a = (_axis_value(ligand, axis_a) + _axis_value(receptor, axis_a)) / 2.0
                mid_b = (_axis_value(ligand, axis_b) + _axis_value(receptor, axis_b)) / 2.0
                px, py = _project_point(
                    a=mid_a,
                    b=mid_b,
                    axis_a=axis_a,
                    axis_b=axis_b,
                    bounds=bounds,
                    width=width,
                    height=height,
                    margin=6,
                )
                if 0 <= px < width and 0 <= py < height:
                    pixels[py * width + px] = (40, 40, 200)
    for px, py, _atom in receptor_points:
        pixels[py * width + px] = (120, 120, 120)
    for px, py, atom in ligand_points:
        pixels[py * width + px] = _ligand_color(atom)
    return pixels


def render_panel_png(
    *,
    receptor_atoms: Sequence[Atom],
    ligand_atoms: Sequence[Atom],
    axis_a: str,
    axis_b: str,
    bounds: tuple[float, float, float, float, float, float],
    width: int = 84,
    height: int = 96,
    draw_contacts: bool = True,
) -> bytes:
    pixels = render_panel_pixels(
        receptor_atoms=receptor_atoms,
        ligand_atoms=ligand_atoms,
        axis_a=axis_a,
        axis_b=axis_b,
        bounds=bounds,
        width=width,
        height=height,
        draw_contacts=draw_contacts,
    )
    return write_png_rgb(width=width, height=height, pixels=pixels)


def render_combined_contact_sheet(
    *,
    receptor_atoms: Sequence[Atom],
    ligand_atoms: Sequence[Atom],
    panel_width: int = 84,
    panel_height: int = 96,
) -> bytes:
    bounds = _shared_bounds(receptor_atoms, ligand_atoms)
    axis_pairs = {"xy": ("x", "y"), "xz": ("x", "z"), "yz": ("y", "z")}
    panel_pixels: list[list[tuple[int, int, int]]] = []
    for name in _PANEL_NAMES:
        axis_a, axis_b = axis_pairs[name]
        panel_pixels.append(
            render_panel_pixels(
                receptor_atoms=receptor_atoms,
                ligand_atoms=ligand_atoms,
                axis_a=axis_a,
                axis_b=axis_b,
                bounds=bounds,
                width=panel_width,
                height=panel_height,
                draw_contacts=True,
            )
        )

    divider = (230, 230, 230)
    total_width = panel_width * 3 + 2
    stitched: list[tuple[int, int, int]] = []
    for row in range(panel_height):
        for panel_index, panel in enumerate(panel_pixels):
            if panel_index:
                stitched.append(divider)
            row_start = row * panel_width
            stitched.extend(panel[row_start : row_start + panel_width])
    return write_png_rgb(width=total_width, height=panel_height, pixels=stitched)


def render_shared_frame_panels(
    *,
    receptor_atoms: Sequence[Atom],
    ligand_atoms: Sequence[Atom],
    width: int = 84,
    height: int = 96,
) -> dict[str, bytes]:
    combined = render_combined_contact_sheet(
        receptor_atoms=receptor_atoms,
        ligand_atoms=ligand_atoms,
        panel_width=width,
        panel_height=height,
    )
    return {"contact_sheet.png": combined}


def build_choice_evidence(
    *,
    choice_id: str,
    cluster_id: str,
    is_rep: bool,
    attachment_index: int,
    descriptors: dict[str, str],
    pose_bytes: bytes,
    protein_bytes: bytes,
    pocket_bytes: bytes,
) -> tuple[dict[str, object], dict[str, bytes]]:
    pose_atoms = parse_pdb_atoms(pose_bytes, label=f"{choice_id}/pose")
    protein_atoms = parse_pdb_atoms(
        protein_bytes,
        label=f"{choice_id}/protein",
        max_atoms=MAX_PROTEIN_ATOMS,
    )
    pocket_atoms = parse_pdb_atoms(pocket_bytes, label=f"{choice_id}/pocket")
    if not pose_atoms:
        raise WeeklyLlmEvidenceError(f"{choice_id} pose has no heavy atoms")
    if not pocket_atoms:
        raise WeeklyLlmEvidenceError(f"{choice_id} pocket has no heavy atoms")
    receptor_atoms = pocket_atoms
    geometry = compute_geometry_metrics(
        pose_atoms=pose_atoms,
        protein_atoms=protein_atoms,
        pocket_atoms=pocket_atoms,
    )
    contacts = _build_nearest_contacts(receptor_atoms, pose_atoms)
    contact_summary = _contact_summaries(receptor_atoms, pose_atoms)
    images = render_shared_frame_panels(receptor_atoms=receptor_atoms, ligand_atoms=pose_atoms)
    image_digests = [
        {
            "attachment_index": attachment_index,
            "filename": filename,
            "sha256": sha256_hex(content),
        }
        for filename in sorted(images)
        for content in [images[filename]]
    ]
    evidence = {
        "choice_id": choice_id,
        "cluster_id": cluster_id,
        "is_rep": is_rep,
        "attachment_index": attachment_index,
        "descriptors": {
            "pose_uri": descriptors["pose_uri"],
            "protein_uri": descriptors["protein_uri"],
            "pocket_uri": descriptors["pocket_uri"],
        },
        "geometry": geometry,
        "nearest_contacts": contacts,
        "contact_summary": contact_summary,
        "attachments": image_digests,
    }
    return evidence, images


__all__ = [
    "Atom",
    "MAX_CONTACT_ENTRIES",
    "MAX_EVIDENCE_JSON_BYTES",
    "MAX_PAIR_CHECKS",
    "WeeklyLlmEvidenceError",
    "build_choice_evidence",
    "compute_geometry_metrics",
    "parse_pdb_atoms",
    "render_combined_contact_sheet",
    "render_shared_frame_panels",
    "write_png_rgb",
]
