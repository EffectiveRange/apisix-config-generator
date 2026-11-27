"""Compose the OpenAPI generator and security provider for public entry points."""

import argparse
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.error import URLError

import yaml
from context_logger import setup_logging

from .generator import OpenapiConfigGenerator, StandaloneConfig
from .security import OpenapiSecurityProvider


def load_document(source: str | Path) -> dict[str, object]:
    """Load a document using the default OpenAPI generator."""
    return OpenapiConfigGenerator(OpenapiSecurityProvider()).load_document(source)


def generate(routing: Mapping[str, object], spec: Mapping[str, object]) -> StandaloneConfig:
    """Generate routes using the default OpenAPI security provider."""
    return OpenapiConfigGenerator(OpenapiSecurityProvider()).generate(routing, spec)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate APISIX standalone routes from an OpenAPI document and routing map."
    )
    parser.add_argument("--routing", type=Path, default="routing.yaml")
    parser.add_argument("--output", type=Path, default="apisix.yaml")
    args = parser.parse_args(argv)
    setup_logging("apisix-config-generator", warn_on_overwrite=False)
    generator = OpenapiConfigGenerator(OpenapiSecurityProvider())
    try:
        generator.generate_file(args.routing, args.output)
    except (OSError, URLError, ValueError, yaml.YAMLError) as error:
        parser.exit(1, f"Error: {error}\n")
    print(f"Generated {args.output}")
    return 0
