"""Deterministic PDB evidence metrics and orthographic PNG rendering."""

from __future__ import annotations

import math
import re
import struct
import zlib
from dataclasses import dataclass
from typing import Iterable, Sequence

from .weekly_llm_contract import sha256_hex

MAX_PDB_BYTES = 5_000_000
MAX_PNG_BYTES = 2_000_000
MAX_IMAGE_DIMENSION = 512

_CLASH_CUTOFF_ANGSTROM = 2.0
_CLOSE_CONTACT_CUTOFF_ANGSTROM = 4.0
_NO_CONTACT_CUTOFF_ANGSTROM = 3.5

_ATOM_RE = re.compile(
    r"^(ATOM|HETATM)\s+\d+\s+\S+\s+(\S+)\s+(\S)\s+(\d+)\s+"
    r"(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)"
)


@dataclass(frozen=True)
class Atom:
    chain_id: str
    res_name: str
    serial: int
    x: float
    y: float
    z: float
    element: str


class WeeklyLlmEvidenceError(ValueError):
    """Raised when evidence generation violates deterministic bounds."""


def parse_pdb_atoms(content: bytes, *, label: str) -> list[Atom]:
    if len(content) > MAX_PDB_BYTES:
        raise WeeklyLlmEvidenceError(f"{label} exceeds {MAX_PDB_BYTES} bytes")
    atoms: list[Atom] = []
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.decode("utf-8", errors="strict")
        match = _ATOM_RE.match(line)
        if not match:
            if line.startswith(("ATOM", "HETATM")):
                raise WeeklyLlmEvidenceError(
                    f"{label} line {line_number} is not a valid finite-coordinate PDB record"
                )
            continue
        record, res_name, chain_id, serial_text, x_text, y_text, z_text = match.groups()
        x, y, z = float(x_text), float(y_text), float(z_text)
        for coord in (x, y, z):
            if not math.isfinite(coord) or abs(coord) > 10_000:
                raise WeeklyLlmEvidenceError(
                    f"{label} line {line_number} has non-finite or out-of-range coordinates"
                )
        element = line[76:78].strip() if len(line) >= 78 else ""
        if not element:
            element = res_name[0:1]
        atoms.append(
            Atom(
                chain_id=chain_id,
                res_name=res_name,
                serial=int(serial_text),
                x=x,
                y=y,
                z=z,
                element=element.upper(),
            )
        )
    atoms.sort(key=lambda atom: (atom.chain_id, atom.res_name, atom.serial, atom.x, atom.y, atom.z))
    return atoms


def centroid(atoms: Sequence[Atom]) -> tuple[float, float, float]:
    if not atoms:
        return (0.0, 0.0, 0.0)
    xs = [atom.x for atom in atoms]
    ys = [atom.y for atom in atoms]
    zs = [atom.z for atom in atoms]
    count = float(len(atoms))
    return (sum(xs) / count, sum(ys) / count, sum(zs) / count)


def _distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def _pairwise_metrics(
    receptor: Sequence[Atom], ligand: Sequence[Atom]
) -> dict[str, int | float]:
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
            if distance < _NO_CONTACT_CUTOFF_ANGSTROM and left.element in {"N", "O"} and right.element in {"N", "O"}:
                possible_n_o_contact_count += 1
    if not math.isfinite(min_distance):
        min_distance = 0.0
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
    pose_centroid = centroid(pose_atoms)
    pocket_centroid = centroid(pocket_atoms if pocket_atoms else protein_atoms)
    metrics = _pairwise_metrics(protein_atoms or pocket_atoms, pose_atoms)
    metrics.update(
        {
            "pose_atom_count": len(pose_atoms),
            "protein_atom_count": len(protein_atoms),
            "pocket_atom_count": len(pocket_atoms),
            "centroid_distance_angstrom": round(_distance(pose_centroid, pocket_centroid), 3),
        }
    )
    return metrics


def write_png_rgb(
    *,
    width: int,
    height: int,
    pixels: Iterable[tuple[int, int, int]],
) -> bytes:
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


def _project_points(
    atoms: Sequence[Atom],
    *,
    width: int,
    height: int,
    margin: int = 8,
) -> list[tuple[int, int]]:
    if not atoms:
        return []
    xs = [atom.x for atom in atoms]
    ys = [atom.y for atom in atoms]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max(max_x - min_x, 1e-6)
    span_y = max(max_y - min_y, 1e-6)
    drawable_w = max(width - 2 * margin, 1)
    drawable_h = max(height - 2 * margin, 1)
    points: list[tuple[int, int]] = []
    for atom in atoms:
        px = margin + int(((atom.x - min_x) / span_x) * (drawable_w - 1))
        py = margin + int(((atom.y - min_y) / span_y) * (drawable_h - 1))
        points.append((px, height - 1 - py))
    return points


def render_orthographic_png(
    *,
    receptor_atoms: Sequence[Atom],
    ligand_atoms: Sequence[Atom],
    width: int = 128,
    height: int = 128,
) -> bytes:
    pixels = [(255, 255, 255)] * (width * height)
    for px, py in _project_points(receptor_atoms, width=width, height=height):
        pixels[py * width + px] = (120, 120, 120)
    for px, py in _project_points(ligand_atoms, width=width, height=height):
        pixels[py * width + px] = (220, 40, 40)
    return write_png_rgb(width=width, height=height, pixels=pixels)


def render_contact_sheet_png(
    *,
    receptor_atoms: Sequence[Atom],
    ligand_atoms: Sequence[Atom],
    width: int = 128,
    height: int = 128,
) -> bytes:
    pixels = [(250, 250, 250)] * (width * height)
    contacts: list[tuple[int, int]] = []
    for ligand in ligand_atoms:
        ligand_point = _project_points([ligand], width=width, height=height)
        if not ligand_point:
            continue
        lx, ly = ligand_point[0]
        for receptor in receptor_atoms:
            if _distance((ligand.x, ligand.y, ligand.z), (receptor.x, receptor.y, receptor.z)) >= _CLOSE_CONTACT_CUTOFF_ANGSTROM:
                continue
            receptor_point = _project_points([receptor], width=width, height=height)
            if not receptor_point:
                continue
            rx, ry = receptor_point[0]
            contacts.append((min(lx, rx), min(ly, ry)))
            contacts.append((max(lx, rx), max(ly, ry)))
    for px, py in _project_points(receptor_atoms, width=width, height=height):
        pixels[py * width + px] = (180, 180, 180)
    for px, py in _project_points(ligand_atoms, width=width, height=height):
        pixels[py * width + px] = (200, 60, 60)
    for px, py in contacts:
        if 0 <= px < width and 0 <= py < height:
            pixels[py * width + px] = (40, 40, 200)
    return write_png_rgb(width=width, height=height, pixels=pixels)


def build_choice_evidence(
    *,
    choice_id: str,
    cluster_id: str,
    is_rep: bool,
    descriptors: dict[str, str],
    pose_bytes: bytes,
    protein_bytes: bytes,
    pocket_bytes: bytes,
) -> tuple[dict[str, object], dict[str, bytes]]:
    pose_atoms = parse_pdb_atoms(pose_bytes, label=f"{choice_id}/pose")
    protein_atoms = parse_pdb_atoms(protein_bytes, label=f"{choice_id}/protein")
    pocket_atoms = parse_pdb_atoms(pocket_bytes, label=f"{choice_id}/pocket")
    geometry = compute_geometry_metrics(
        pose_atoms=pose_atoms,
        protein_atoms=protein_atoms,
        pocket_atoms=pocket_atoms,
    )
    overview = render_orthographic_png(receptor_atoms=protein_atoms or pocket_atoms, ligand_atoms=pose_atoms)
    contact_sheet = render_contact_sheet_png(receptor_atoms=protein_atoms or pocket_atoms, ligand_atoms=pose_atoms)
    images = {
        "overview.png": overview,
        "contact_sheet.png": contact_sheet,
    }
    evidence = {
        "choice_id": choice_id,
        "cluster_id": cluster_id,
        "is_rep": is_rep,
        "descriptors": {
            "pose_uri": descriptors["pose_uri"],
            "protein_uri": descriptors["protein_uri"],
            "pocket_uri": descriptors["pocket_uri"],
        },
        "geometry": geometry,
        "images": {
            "overview_png_sha256": sha256_hex(overview),
            "contact_sheet_png_sha256": sha256_hex(contact_sheet),
        },
    }
    return evidence, images


__all__ = [
    "Atom",
    "WeeklyLlmEvidenceError",
    "build_choice_evidence",
    "compute_geometry_metrics",
    "parse_pdb_atoms",
    "render_contact_sheet_png",
    "render_orthographic_png",
    "write_png_rgb",
]
