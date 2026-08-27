"""Prediction method adapters."""

from .boltz2 import Boltz2Adapter
from .openfold3 import OpenFold3Adapter

ADAPTERS = {
    "boltz2": Boltz2Adapter(),
    "openfold3": OpenFold3Adapter(),
}

__all__ = ["ADAPTERS", "Boltz2Adapter", "OpenFold3Adapter"]
