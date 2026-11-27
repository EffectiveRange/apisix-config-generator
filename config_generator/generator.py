#!/usr/bin/env python3
"""Generate APISIX standalone routes from an OpenAPI document and routing map."""

import argparse
import copy
import re
import sys
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NotRequired, TypedDict
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

import yaml
from context_logger import get_logger, setup_logging
from structlog.stdlib import BoundLogger

HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
ENVIRONMENT_VARIABLE = r"\$\{\{[A-Za-z_][A-Za-z0-9_]*}}"


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


@dataclass(frozen=True)
class Operation:
    path: str
    method: str
    security: object
    security_declared: bool


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

            operation = available[operation_id]
            path, method = operation.path, operation.method
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
            authentication = self._authentication(operation.security, routing, spec, operation_id)
            if operation.security_declared and "openid-connect" in plugins:
                raise ConfigError(
                    f"Route {operation_id} openid-connect plugin conflicts with OpenAPI security; "
                    "configure security_schemes instead"
                )

            if authentication is not None:
                plugins["openid-connect"] = authentication

            if plugins:
                route["plugins"] = plugins

            routes.append(route)

            self._log.info("Route generated", operation_id=operation_id, method=method, uri=uri, service=service)

        self._log.info("Configuration generated", route_count=len(routes))
        return {"routes": routes}

    def _operations(self, spec: Mapping[str, object]) -> dict[str, Operation]:
        if not isinstance(version := spec.get("openapi"), str) or not version.startswith("3."):
            raise ConfigError("api.spec must be an OpenAPI 3.x document")

        result: dict[str, Operation] = {}

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

                result[operation_id] = Operation(
                    path,
                    method.upper(),
                    operation.get("security", spec.get("security", [])),
                    "security" in operation or "security" in spec,
                )

        return result

    def _authentication(
        self,
        security: object | dict[str, object],
        routing: Mapping[str, object],
        spec: Mapping[str, object],
        operation_id: str,
    ) -> dict[str, object] | None:
        label = f"Security for {operation_id}"
        if not isinstance(security, list):
            raise ConfigError(f"{label} must be an array")

        if not security:
            return None

        if len(security) != 1:
            raise ConfigError(f"{label}: alternative security requirements are not supported")

        if not (requirement := mapping(security[0], label)):
            return None

        if len(requirement) != 1:
            raise ConfigError(f"{label}: combined security schemes are not supported")

        name, raw_scopes = next(iter(requirement.items()))
        scopes = self._scopes(raw_scopes, label)
        components = mapping(spec.get("components", {}), "OpenAPI components")
        schemes = mapping(components.get("securitySchemes", {}), "OpenAPI securitySchemes")

        if name not in schemes:
            raise ConfigError(f"{label} references unknown security scheme: {name}")

        scheme = mapping(schemes[name], f"Security scheme {name}")
        self._validate_security_scheme(scheme, scopes, name)
        configured = mapping(routing.get("security_schemes", {}), "security_schemes")

        if name not in configured:
            raise ConfigError(f"Security scheme {name} requires IDP settings in routing.security_schemes")

        settings = mapping(configured[name], f"IDP settings for {name}")
        return self._openid_connect(settings, scheme, scopes, name)

    def _scopes(self, value: object | list[object], label: str) -> list[str]:
        if not isinstance(value, list):
            raise ConfigError(f"{label} scopes must be an array")

        scopes: list[str] = []

        for scope in value:
            if not isinstance(scope, str) or not re.fullmatch(r"[\x21\x23-\x5B\x5D-\x7E]+", scope):
                raise ConfigError(f"{label} contains an invalid scope")

            if scope not in scopes:
                scopes.append(scope)

        return scopes

    def _validate_security_scheme(self, scheme: dict[str, object], scopes: list[str], name: str) -> None:
        if "$ref" in scheme:
            raise ConfigError(f"Security scheme {name}: $ref is not supported")

        if (kind := scheme.get("type")) == "http" and scheme.get("scheme") == "bearer":
            if scopes:
                raise ConfigError(f"HTTP bearer security scheme {name} must have empty scopes")
        elif kind == "openIdConnect":
            self._https_endpoint(scheme.get("openIdConnectUrl"), f"Security scheme {name} openIdConnectUrl")
        elif kind == "oauth2":
            self._validate_oauth_scopes(scheme, scopes, name)
        else:
            raise ConfigError(f"Security scheme {name}: only HTTP bearer, oauth2 and openIdConnect are supported")

    def _validate_oauth_scopes(self, scheme: dict[str, object], scopes: list[str], name: str) -> None:
        if not (flows := mapping(scheme.get("flows"), f"OAuth2 flows for {name}")) or flows.keys() - {
            "implicit",
            "password",
            "clientCredentials",
            "authorizationCode",
        }:
            raise ConfigError(f"OAuth2 security scheme {name} must define supported flows")

        declared: set[str] = set()

        for flow_name, raw_flow in flows.items():
            flow = mapping(raw_flow, f"OAuth2 flow {flow_name} for {name}")
            declared.update(mapping(flow.get("scopes"), f"OAuth2 scopes for {name}"))

        if set(scopes) - declared:
            raise ConfigError(f"Security scheme {name} requires undeclared OAuth2 scopes")

    def _openid_connect(
        self, settings: dict[str, object], scheme: dict[str, object], scopes: list[str], name: str
    ) -> dict[str, object]:
        allowed = {
            "client_id",
            "client_secret",
            "discovery",
            "introspection_endpoint",
            "introspection_endpoint_auth_method",
            "use_jwks",
        }

        if settings.keys() - allowed:
            raise ConfigError(f"IDP settings for {name} contain unsupported fields")

        label = f"IDP settings for {name}"

        if not isinstance(use_jwks := settings.get("use_jwks", True), bool):
            raise ConfigError(f"{label} use_jwks must be a boolean")

        if use_jwks and settings.keys() & {"introspection_endpoint", "introspection_endpoint_auth_method"}:
            raise ConfigError(f"{label} cannot combine use_jwks with introspection settings")

        if (method := settings.get("introspection_endpoint_auth_method", "client_secret_basic")) not in (
            "client_secret_basic",
            "client_secret_post",
        ):
            raise ConfigError(
                f"{label} introspection_endpoint_auth_method must be client_secret_basic or client_secret_post"
            )

        result: dict[str, object] = {
            "client_id": self._idp_string(settings.get("client_id"), f"{label} client_id"),
            "client_secret": self._idp_string(settings.get("client_secret"), f"{label} client_secret"),
            "discovery": self._https_endpoint(
                settings.get("discovery", scheme.get("openIdConnectUrl")), f"{label} discovery"
            ),
            "bearer_only": True,
            "unauth_action": "deny",
            "ssl_verify": True,
            "required_scopes": scopes,
            "cache_segment": f"security:{name}",
        }

        if use_jwks:
            result.update(
                {
                    "use_jwks": True,
                    "accept_none_alg": False,
                    "accept_unsupported_alg": False,
                    "claim_validator": {
                        "audience": {"required": True, "match_with_client_id": True},
                    },
                }
            )
        else:
            result["introspection_endpoint_auth_method"] = method

        if "introspection_endpoint" in settings:
            result["introspection_endpoint"] = self._https_endpoint(
                settings["introspection_endpoint"], f"{label} introspection_endpoint"
            )

        return result

    def _idp_string(self, value: object | str, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{label} must be a nonempty string")

        if "${" in value and not re.fullmatch(ENVIRONMENT_VARIABLE, value):
            raise ConfigError(f"{label} must use a complete APISIX environment placeholder")

        return value

    def _https_endpoint(self, value: object, label: str) -> str:
        if re.fullmatch(ENVIRONMENT_VARIABLE, endpoint := self._idp_string(value, label)):
            return endpoint

        parsed = urlsplit(endpoint)

        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(character.isspace() for character in endpoint)
        ):
            raise ConfigError(f"{label} must be an HTTPS URL")

        if parsed.port == 0:
            raise ConfigError(f"{label} has an invalid port")

        return endpoint

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

        if placeholder := re.fullmatch(rf"(https?)://({ENVIRONMENT_VARIABLE})/?", url):
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
