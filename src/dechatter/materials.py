"""Starting-point cutting data. These only bound the test grid; the cuts are the measurement.

Values are conservative handbook-style starting points, not recommendations. Override
`chip_load_mm` and the speed range in `setup.toml` when you know better.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import ConfigError, ToolMaterial


@dataclass(frozen=True)
class Material:
    surface_speed_m_min: float  # carbide
    chip_load_per_diameter: float  # feed per tooth as a fraction of tool diameter
    specific_energy_j_mm3: float  # spindle energy per volume removed


MATERIALS: dict[str, Material] = {
    "acetal": Material(300, 0.012, 0.25),
    "aluminum-6061": Material(300, 0.010, 0.70),
    "brass-360": Material(150, 0.008, 1.20),
    "cast-iron": Material(90, 0.006, 1.60),
    "steel-1018": Material(120, 0.006, 2.20),
    "steel-4140": Material(90, 0.005, 2.80),
    "stainless-304": Material(60, 0.004, 3.00),
    "titanium-6al4v": Material(50, 0.004, 3.50),
}

HSS_SPEED_FACTOR = 0.3


def lookup(name: str) -> Material:
    if name not in MATERIALS:
        raise ConfigError(f"unknown material {name!r}; known: {', '.join(sorted(MATERIALS))}")
    return MATERIALS[name]


def surface_speed_m_min(material: Material, tool_material: ToolMaterial) -> float:
    if tool_material is ToolMaterial.HSS:
        return material.surface_speed_m_min * HSS_SPEED_FACTOR
    return material.surface_speed_m_min


def weight_derate(weight_kg: float) -> float:
    """Chip-load derate for light machines. Heuristic: lighter frames deflect more."""
    if weight_kg < 150:
        return 0.5
    if weight_kg < 400:
        return 0.7
    if weight_kg < 1500:
        return 0.85
    return 1.0
