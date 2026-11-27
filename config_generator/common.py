"""Shared configuration validation."""

ENVIRONMENT_VARIABLE = r"\$\{\{[A-Za-z_][A-Za-z0-9_]*}}"


class ConfigError(ValueError):
    """An input cannot be represented by this generator."""


def mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be a mapping")

    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ConfigError(f"{label} must have string keys")
        result[key] = item
    return result
