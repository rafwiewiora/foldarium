"""Provider-neutral catalog shapes for model resolution without cursor-sdk."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence


@dataclass(frozen=True)
class CatalogParameterValue:
    value: str
    display_name: str


@dataclass(frozen=True)
class CatalogParameterDefinition:
    id: str
    display_name: str
    values: tuple[CatalogParameterValue, ...] = ()


@dataclass(frozen=True)
class CatalogParameterSelection:
    id: str
    value: str


@dataclass(frozen=True)
class CatalogVariant:
    display_name: str
    params: tuple[CatalogParameterSelection, ...] = ()


@dataclass(frozen=True)
class CatalogModel:
    id: str
    display_name: str
    parameters: tuple[CatalogParameterDefinition, ...] = ()
    variants: tuple[CatalogVariant, ...] = ()


HIGH_EFFORT_NEEDLE = "high"


class CatalogResolutionError(RuntimeError):
    """Raised when an exact model preset cannot be resolved."""


def resolve_sol_high_model(models: Sequence[CatalogModel]) -> tuple[str, tuple[CatalogParameterSelection, ...]]:
    candidates = [
        model
        for model in models
        if "gpt-5.6" in f"{model.id} {model.display_name}".lower()
        and "sol" in f"{model.id} {model.display_name}".lower()
    ]
    if len(candidates) != 1:
        raise CatalogResolutionError("exact accessible GPT-5.6 Sol model could not be resolved")
    model = candidates[0]
    params = _resolve_high_reasoning_parameters(model)
    return model.id, params


def _resolve_high_reasoning_parameters(
    model: CatalogModel,
) -> tuple[CatalogParameterSelection, ...]:
    for parameter in model.parameters:
        haystack = f"{parameter.id} {parameter.display_name}".lower()
        if "reason" in haystack or "effort" in haystack:
            for value in parameter.values:
                label = f"{value.value} {value.display_name}".lower()
                if HIGH_EFFORT_NEEDLE in label:
                    return (CatalogParameterSelection(id=parameter.id, value=value.value),)
            high_values = [
                value
                for value in parameter.values
                if HIGH_EFFORT_NEEDLE in f"{value.value} {value.display_name}".lower()
            ]
            if len(high_values) == 1:
                return (CatalogParameterSelection(id=parameter.id, value=high_values[0].value),)
    for variant in model.variants:
        if HIGH_EFFORT_NEEDLE in variant.display_name.lower() or any(
            HIGH_EFFORT_NEEDLE in param.value.lower() for param in variant.params
        ):
            return variant.params
    raise CatalogResolutionError("exact high-reasoning parameter for GPT-5.6 Sol could not be resolved")


def catalog_model_from_mapping(raw: Any) -> CatalogModel:
    parameters = tuple(
        CatalogParameterDefinition(
            id=str(parameter.id),
            display_name=str(parameter.display_name),
            values=tuple(
                CatalogParameterValue(value=str(value.value), display_name=str(value.display_name))
                for value in parameter.values
            ),
        )
        for parameter in getattr(raw, "parameters", []) or []
    )
    variants = tuple(
        CatalogVariant(
            display_name=str(variant.display_name),
            params=tuple(
                CatalogParameterSelection(id=str(param.id), value=str(param.value))
                for param in variant.params
            ),
        )
        for variant in getattr(raw, "variants", []) or []
    )
    return CatalogModel(
        id=str(raw.id),
        display_name=str(raw.display_name),
        parameters=parameters,
        variants=variants,
    )


__all__ = [
    "CatalogModel",
    "CatalogParameterDefinition",
    "CatalogParameterSelection",
    "CatalogParameterValue",
    "CatalogResolutionError",
    "CatalogVariant",
    "catalog_model_from_mapping",
    "resolve_sol_high_model",
]
