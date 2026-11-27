"""Translate OpenAPI security requirements into APISIX plugins."""

import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from urllib.parse import urlsplit

from context_logger import get_logger
from structlog.stdlib import BoundLogger

from .common import ENVIRONMENT_VARIABLE, ConfigError, mapping


class SecurityProvider(ABC):
    """Generate security plugins for an operation without modifying inputs."""

    @abstractmethod
    def generate(
        self,
        operation: Mapping[str, object],
        routing: Mapping[str, object],
        spec: Mapping[str, object],
        operation_id: str,
        plugins: Mapping[str, object],
    ) -> dict[str, object]:
        """Return security plugins, validating conflicts with configured plugins."""


class OpenapiSecurityProvider(SecurityProvider):
    """Map OpenAPI bearer, OAuth2 and OIDC requirements to openid-connect."""

    def __init__(self) -> None:
        self._log: BoundLogger = get_logger(type(self).__name__)

    def generate(
        self,
        operation: Mapping[str, object],
        routing: Mapping[str, object],
        spec: Mapping[str, object],
        operation_id: str,
        plugins: Mapping[str, object],
    ) -> dict[str, object]:
        self._log.debug("Generating security plugins", operation_id=operation_id)
        authentication = self._authentication(
            operation.get("security", spec.get("security", [])), routing, spec, operation_id
        )
        if ("security" in operation or "security" in spec) and "openid-connect" in plugins:
            raise ConfigError(
                f"Route {operation_id} openid-connect plugin conflicts with OpenAPI security; "
                "configure security instead"
            )
        if authentication is None:
            self._log.debug("No security plugin required", operation_id=operation_id)
            return {}
        self._log.info(
            "Security plugin generated",
            operation_id=operation_id,
            plugin="openid-connect",
            validation_mode="jwks" if authentication.get("use_jwks") else "introspection",
            required_scopes=authentication["required_scopes"],
        )
        return {"openid-connect": authentication}

    def _authentication(
        self,
        security: object,
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
        configured = mapping(routing.get("security", {}), "security")
        if name not in configured:
            raise ConfigError(f"Security scheme {name} requires IDP settings in routing.security")
        settings = mapping(configured[name], f"IDP settings for {name}")
        return self._openid_connect(settings, scheme, scopes, name)

    def _scopes(self, value: object, label: str) -> list[str]:
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
                    "claim_validator": {"audience": {"required": True, "match_with_client_id": True}},
                }
            )
        else:
            result["introspection_endpoint_auth_method"] = method
        if "introspection_endpoint" in settings:
            result["introspection_endpoint"] = self._https_endpoint(
                settings["introspection_endpoint"], f"{label} introspection_endpoint"
            )
        return result

    def _idp_string(self, value: object, label: str) -> str:
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
