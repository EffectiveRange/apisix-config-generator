#!/usr/bin/env python3
"""Generate APISIX standalone routes from an OpenAPI document and routing map."""

import argparse
import copy
import re
import sys
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NotRequired, TypedDict
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

import yaml
from context_logger import get_logger, setup_logging
from structlog.stdlib import BoundLogger

HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


class ConfigError(ValueError):
    """An input cannot be represented by this generator."""


class UpstreamConfig(TypedDict):
    type: str
    scheme: str
    nodes: dict[str, int]
    pass_host: str


class RouteConfig(TypedDict):
    id: str
    uri: str
    methods: list[str]
    upstream: UpstreamConfig
    plugins: NotRequired[dict[str, object]]


class StandaloneConfig(TypedDict):
    routes: list[RouteConfig]


def mapping(value: object | dict[str, object], label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be a mapping")

    result: dict[str, object] = {}

    for key, item in value.items():
        if not isinstance(key, str):
            raise ConfigError(f"{label} must have string keys")
        result[key] = item

    return result


class ConfigGenerator(ABC):
    """Generate APISIX standalone configuration from routing and API documents."""

    def __init__(self) -> None:
        self._log: BoundLogger = get_logger(type(self).__name__)

    @abstractmethod
    def generate(self, routing: Mapping[str, object], spec: Mapping[str, object]) -> StandaloneConfig:
        """Validate documents and generate routes without modifying the inputs."""

    def load_document(self, source: str | Path) -> dict[str, object]:
        self._log.debug("Loading document", source=str(source))

        try:
            if isinstance(source, str) and source.startswith(("https://", "http://")):
                with urlopen(source, timeout=30) as response:
                    content = response.read().decode("utf-8-sig")
            else:
                content = Path(source).read_text(encoding="utf-8-sig")
            return mapping(yaml.safe_load(content), str(source))
        except (OSError, URLError, ValueError, yaml.YAMLError) as error:
            self._log.error("Document loading failed", error=str(error), source=str(source))
            raise

    def generate_file(self, routing_path: Path, output_path: Path) -> None:
        routing = self.load_document(routing_path)

        try:
            if not isinstance(source := mapping(routing.get("api"), "api").get("spec"), str) or not source:
                raise ConfigError("api.spec must be a local path or HTTP(S) URL")

            spec_source: str | Path = source
            if not source.startswith(("https://", "http://")):
                spec_source = routing_path.resolve().parent / source
        except (OSError, ValueError) as error:
            self._log.error(
                "Configuration generation failed", error=str(error), routing=str(routing_path), output=str(output_path)
            )
            raise

        config = self.generate(routing, self.load_document(spec_source))

        self.write_config(config, output_path)

    def write_config(self, config: StandaloneConfig, output_path: Path) -> None:
        try:
            self._write_config(config, output_path)
        except (OSError, ValueError, yaml.YAMLError) as error:
            self._log.error("Configuration writing failed", error=str(error), output=str(output_path))
            raise

    def _write_config(self, config: StandaloneConfig, output_path: Path) -> None:
        content = yaml.safe_dump(config, sort_keys=False) + "#END\n"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # APISIX only reloads complete files; replace rather than write in place.
        temporary: Path | None = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=output_path.parent,
                prefix=output_path.name + ".",
                suffix=".tmp",
                delete=False,
            ) as output:
                temporary = Path(output.name)
                output.write(content)
            temporary.replace(output_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

        self._log.info("Configuration written", output=str(output_path))


class OpenapiConfigGenerator(ConfigGenerator):
    """Generate APISIX routes for selected OpenAPI 3.x operations."""

    def generate(self, routing: Mapping[str, object], spec: Mapping[str, object]) -> StandaloneConfig:
        try:
            return self._generate(routing, spec)
        except ValueError as error:
            self._log.error("Configuration generation failed", error=str(error))
            raise

    def _generate(self, routing: Mapping[str, object], spec: Mapping[str, object]) -> StandaloneConfig:
        if not (configured_routes := mapping(routing.get("routes"), "routes")):
            raise ConfigError("routes must not be empty")

        self._log.info("Generating configuration", route_count=len(configured_routes))

        services = mapping(routing.get("services"), "services")
        defaults = mapping(routing.get("plugins", {}), "plugins")
        available = self._operations(spec)
        routes: list[RouteConfig] = []
        matches: set[tuple[str, str]] = set()

        for operation_id, raw_settings in configured_routes.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", operation_id):
                raise ConfigError(
                    f"operationId {operation_id!r} must be a valid APISIX ID "
                    "(1-64 letters, digits, underscores or hyphens)"
                )

            if operation_id not in available:
                raise ConfigError(f"Unknown operationId: {operation_id}")

            settings = mapping(raw_settings, f"Route {operation_id}")
            if not isinstance(service := settings.get("service"), str) or service not in services:
                raise ConfigError(f"Route {operation_id} references unknown service: {service!r}")

            path, method = available[operation_id]
            uri = self._route_uri(path)
            # Parameter names do not distinguish otherwise identical route matches.
            if (match := (re.sub(r":[^/]+", ":", uri), method)) in matches:
                raise ConfigError(f"Duplicate route match: {method} {path}")

            matches.add(match)
            route: RouteConfig = {
                "id": operation_id,
                "uri": uri,
                "methods": [method],
                "upstream": self._upstream(services[service], service),
            }

            plugins = copy.deepcopy(defaults)
            plugins.update(mapping(settings.get("plugins", {}), f"Plugins for {operation_id}"))
            if plugins:
                route["plugins"] = plugins

            routes.append(route)

            self._log.info("Route generated", operation_id=operation_id, method=method, uri=uri, service=service)

        self._log.info("Configuration generated", route_count=len(routes))
        return {"routes": routes}

    def _operations(self, spec: Mapping[str, object]) -> dict[str, tuple[str, str]]:
        if not isinstance(version := spec.get("openapi"), str) or not version.startswith("3."):
            raise ConfigError("api.spec must be an OpenAPI 3.x document")

        result: dict[str, tuple[str, str]] = {}

        for path, raw_item in mapping(spec.get("paths"), "OpenAPI paths").items():
            if not path.startswith("/"):
                raise ConfigError(f"Invalid OpenAPI path: {path!r}")

            item = mapping(raw_item, f"Path {path}")
            if "$ref" in item:
                raise ConfigError(f"Path item $ref is not supported: {path}")

            for method, raw_operation in item.items():
                if method not in HTTP_METHODS:
                    continue

                operation = mapping(raw_operation, f"{method.upper()} {path}")

                if (operation_id := operation.get("operationId")) is None:
                    continue

                if not isinstance(operation_id, str) or not operation_id:
                    raise ConfigError(f"Invalid operationId for {method.upper()} {path}")

                if operation_id in result:
                    raise ConfigError(f"Duplicate operationId: {operation_id}")

                result[operation_id] = (path, method.upper())

        return result

    def _route_uri(self, path: str) -> str:
        segments: list[str] = []

        for segment in path.split("/"):
            if parameter := re.fullmatch(r"\{([A-Za-z_][A-Za-z0-9_]*)}", segment):
                segments.append(f":{parameter[1]}")

            elif any(character in segment for character in "{}:*"):
                raise ConfigError(f"Unsupported path segment {segment!r} in {path}")

            else:
                segments.append(segment)

        return "/".join(segments)

    def _upstream(self, raw_service: object, name: str) -> UpstreamConfig:
        service = mapping(raw_service, f"Service {name}")

        if not isinstance(url := service.get("url"), str):
            raise ConfigError(f"Service {name} requires a URL")

        if placeholder := re.fullmatch(r"(https?)://(\$\{\{[A-Za-z_][A-Za-z0-9_]*}})/?", url):
            scheme, node = placeholder[1], placeholder[2]
        else:
            if any(character in url for character in "${}"):
                raise ConfigError(
                    f"Service {name} environment URL must be "
                    "http://${{NAME}} or https://${{NAME}} with a valid environment variable name"
                )
            scheme, node = self._origin(url, name)
        return {"type": "roundrobin", "scheme": scheme, "nodes": {node: 1}, "pass_host": "pass"}

    def _origin(self, url: str, name: str) -> tuple[str, str]:
        parsed = urlsplit(url)
        host = parsed.hostname

        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ConfigError(f"Service {name} URL must be an HTTP(S) origin: {url}")

        if (port := parsed.port) is None:
            port = 443 if parsed.scheme == "https" else 80

        if port == 0 or any(character.isspace() for character in url):
            raise ConfigError(f"Invalid service {name} URL: {url}")

        if ":" in host:
            host = f"[{host}]"

        return parsed.scheme, f"{host}:{port}"


def load_document(source: str | Path) -> dict[str, object]:
    """Load a document using the default OpenAPI generator."""
    return OpenapiConfigGenerator().load_document(source)


def generate(routing: Mapping[str, object], spec: Mapping[str, object]) -> StandaloneConfig:
    """Generate routes using the default OpenAPI generator."""
    return OpenapiConfigGenerator().generate(routing, spec)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routing", type=Path, default="routing.yaml")
    parser.add_argument("--output", type=Path, default="apisix.yaml")
    args = parser.parse_args(argv)

    setup_logging("apisix-config-generator", warn_on_overwrite=False)

    generator = OpenapiConfigGenerator()

    try:
        generator.generate_file(args.routing, args.output)
    except (OSError, URLError, ValueError, yaml.YAMLError) as error:
        parser.exit(1, f"Error: {error}\n")

    print(f"Generated {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
