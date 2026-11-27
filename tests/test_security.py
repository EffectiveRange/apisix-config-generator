import copy
import tempfile
from pathlib import Path
from typing import Any
from unittest import TestCase
from unittest.mock import patch

import yaml
from context_logger import setup_logging

from config_generator import ConfigError, OpenapiConfigGenerator, generate


class TestSecurity(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        setup_logging("apisix-config-generator", "DEBUG", warn_on_overwrite=False)

    def setUp(self) -> None:
        print()
        self.generator = OpenapiConfigGenerator()
        self.operation: dict[str, Any] = {"operationId": "getCamera", "security": [{"authentik": ["cameras:read"]}]}
        self.scheme: dict[str, Any] = {
            "type": "oauth2",
            "flows": {
                "authorizationCode": {
                    "authorizationUrl": "https://idp.example/application/o/authorize/",
                    "tokenUrl": "https://idp.example/application/o/token/",
                    "scopes": {"cameras:read": "Read cameras", "cameras:write": "Write cameras"},
                }
            },
        }
        self.spec: dict[str, Any] = {
            "openapi": "3.1.0",
            "paths": {"/cameras/{cameraId}": {"get": self.operation}},
            "components": {"securitySchemes": {"authentik": self.scheme}},
        }
        self.idp: dict[str, Any] = {
            "use_jwks": False,
            "client_id": "${{AUTHENTIK_CLIENT_ID}}",
            "client_secret": "${{AUTHENTIK_CLIENT_SECRET}}",
            "discovery": "https://idp.example/application/o/cameras/.well-known/openid-configuration",
        }
        self.routing: dict[str, Any] = {
            "routes": {"getCamera": {"service": "camera"}},
            "services": {"camera": {"url": "http://camera:8080"}},
            "security_schemes": {"authentik": self.idp},
        }

    def test_oauth2_security_generates_bearer_introspection_plugin(self) -> None:
        # Given
        original = copy.deepcopy((self.routing, self.spec))

        # When
        result = generate(self.routing, self.spec)

        # Then
        self.assertEqual(
            result["routes"][0]["plugins"],
            {
                "openid-connect": {
                    **{key: value for key, value in self.idp.items() if key != "use_jwks"},
                    "bearer_only": True,
                    "unauth_action": "deny",
                    "ssl_verify": True,
                    "introspection_endpoint_auth_method": "client_secret_basic",
                    "required_scopes": ["cameras:read"],
                    "cache_segment": "security:authentik",
                }
            },
        )
        self.assertEqual((self.routing, self.spec), original)

    def test_global_security_is_inherited(self) -> None:
        # Given
        self.spec["security"] = self.operation.pop("security")

        # When
        result = self.generator.generate(self.routing, self.spec)

        # Then
        plugin = result["routes"][0]["plugins"]["openid-connect"]
        assert isinstance(plugin, dict)
        self.assertEqual(plugin["required_scopes"], ["cameras:read"])

    def test_operation_security_overrides_global_scopes(self) -> None:
        # Given
        self.spec["security"] = [{"authentik": ["cameras:write"]}]

        # When
        result = self.generator.generate(self.routing, self.spec)

        # Then
        plugin = result["routes"][0]["plugins"]["openid-connect"]
        assert isinstance(plugin, dict)
        self.assertEqual(plugin["required_scopes"], ["cameras:read"])

    def test_public_operations_do_not_receive_authentication(self) -> None:
        public_security: list[object] = [[], [{}]]
        for security in public_security:
            with self.subTest(security=security):
                print()
                # Given
                self.spec["security"] = [{"authentik": ["cameras:write"]}]
                self.operation["security"] = security
                self.routing["security_schemes"] = {}

                # When
                result = self.generator.generate(self.routing, self.spec)

                # Then
                self.assertNotIn("plugins", result["routes"][0])

    def test_undeclared_security_preserves_manual_plugins(self) -> None:
        # Given
        self.operation.pop("security")
        self.routing["plugins"] = {"openid-connect": {"bearer_only": True}}

        # When
        result = self.generator.generate(self.routing, self.spec)

        # Then
        self.assertEqual(result["routes"][0]["plugins"], self.routing["plugins"])

    def test_bearer_and_oidc_security_schemes(self) -> None:
        for scheme in [
            {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
            {"type": "openIdConnect", "openIdConnectUrl": self.idp["discovery"]},
        ]:
            with self.subTest(scheme=scheme):
                print()
                # Given
                self.spec["components"]["securitySchemes"]["authentik"] = scheme
                self.operation["security"] = [{"authentik": []}]

                # When
                result = self.generator.generate(self.routing, self.spec)

                # Then
                plugin = result["routes"][0]["plugins"]["openid-connect"]
                assert isinstance(plugin, dict)
                self.assertTrue(plugin["bearer_only"])
                self.assertEqual(plugin["required_scopes"], [])

    def test_oidc_discovery_defaults_to_spec(self) -> None:
        # Given
        discovery = self.idp.pop("discovery")
        self.spec["components"]["securitySchemes"]["authentik"] = {
            "type": "openIdConnect",
            "openIdConnectUrl": discovery,
        }

        # When
        result = self.generator.generate(self.routing, self.spec)

        # Then
        plugin = result["routes"][0]["plugins"]["openid-connect"]
        assert isinstance(plugin, dict)
        self.assertEqual(plugin["discovery"], discovery)

    def test_introspection_endpoint_and_auth_method_can_be_configured(self) -> None:
        # Given
        self.idp.update(
            introspection_endpoint="https://idp.example/application/o/introspect/",
            introspection_endpoint_auth_method="client_secret_post",
        )

        # When
        result = self.generator.generate(self.routing, self.spec)

        # Then
        plugin = result["routes"][0]["plugins"]["openid-connect"]
        assert isinstance(plugin, dict)
        self.assertEqual(plugin["introspection_endpoint"], self.idp["introspection_endpoint"])
        self.assertEqual(plugin["introspection_endpoint_auth_method"], "client_secret_post")

    def test_unrelated_plugins_are_preserved_and_routes_are_independent(self) -> None:
        # Given
        self.routing["plugins"] = {"cors": {}}
        self.routing["routes"]["getCamera"]["plugins"] = {"limit-count": {"count": 10}}
        self.routing["routes"]["updateCamera"] = {"service": "camera"}
        self.spec["paths"]["/cameras/{cameraId}"]["put"] = {
            "operationId": "updateCamera",
            "security": [{"authentik": ["cameras:write"]}],
        }

        # When
        result = self.generator.generate(self.routing, self.spec)
        first = result["routes"][0]["plugins"]
        second = result["routes"][1]["plugins"]

        # Then
        self.assertIn("cors", first)
        self.assertIn("limit-count", first)
        self.assertNotIn("limit-count", second)
        first_auth, second_auth = first["openid-connect"], second["openid-connect"]
        assert isinstance(first_auth, dict) and isinstance(second_auth, dict)
        self.assertEqual(first_auth["required_scopes"], ["cameras:read"])
        self.assertEqual(second_auth["required_scopes"], ["cameras:write"])

    def test_security_errors_are_logged_without_disclosing_client_secret(self) -> None:
        # Given
        self.idp["client_secret"] = " "
        with patch("config_generator.generator.get_logger") as get_logger:
            generator = OpenapiConfigGenerator()

            # When
            with self.assertRaises(ConfigError) as error:
                generator.generate(self.routing, self.spec)

            # Then
            get_logger.return_value.error.assert_called_once_with(
                "Configuration generation failed", error=str(error.exception)
            )
            self.assertIn("client_secret must be a nonempty string", str(error.exception))

    def test_invalid_security_requirements_are_rejected(self) -> None:
        cases: list[tuple[object, str]] = [
            (None, "must be an array"),
            ({}, "must be an array"),
            ([None], "must be a mapping"),
            ([{"authentik": []}, {"other": []}], "alternative security requirements"),
            ([{}, {"authentik": []}], "alternative security requirements"),
            ([{"authentik": [], "other": []}], "combined security schemes"),
            ([{"unknown": []}], "unknown security scheme"),
            ([{"authentik": None}], "scopes must be an array"),
            ([{"authentik": [42]}], "invalid scope"),
            ([{"authentik": [""]}], "invalid scope"),
            ([{"authentik": ["two scopes"]}], "invalid scope"),
            ([{"authentik": ["unknown"]}], "undeclared OAuth2 scopes"),
        ]
        for security, message in cases:
            with self.subTest(security=security):
                print()
                # Given
                self.operation["security"] = security

                # When
                with self.assertRaises(ConfigError) as error:
                    self.generator.generate(self.routing, self.spec)

                # Then
                self.assertIn(message, str(error.exception))

    def test_unsupported_or_invalid_security_schemes_are_rejected(self) -> None:
        for scheme, message in [
            ({"type": "apiKey", "in": "header", "name": "X-API-Key"}, "only HTTP bearer"),
            ({"type": "http", "scheme": "basic"}, "only HTTP bearer"),
            ({"$ref": "#/components/securitySchemes/other"}, "$ref is not supported"),
            ({"type": "http", "scheme": "bearer"}, "must have empty scopes"),
            ({"type": "oauth2", "flows": {}}, "must define supported flows"),
            ({"type": "oauth2", "flows": {"invalid": {}}}, "must define supported flows"),
            ({"type": "openIdConnect"}, "must be a nonempty string"),
        ]:
            with self.subTest(scheme=scheme):
                print()
                # Given
                self.spec["components"]["securitySchemes"]["authentik"] = scheme

                # When
                with self.assertRaises(ConfigError) as error:
                    self.generator.generate(self.routing, self.spec)

                # Then
                self.assertIn(message, str(error.exception))

    def test_missing_idp_configuration_is_rejected(self) -> None:
        # Given
        self.routing.pop("security_schemes")

        # When
        with self.assertRaises(ConfigError) as error:
            self.generator.generate(self.routing, self.spec)

        # Then
        self.assertIn("requires IDP settings", str(error.exception))

    def test_invalid_idp_settings_are_rejected(self) -> None:
        cases: list[tuple[str, object]] = [
            ("client_id", None),
            ("client_secret", ""),
            ("client_secret", "${AUTHENTIK_CLIENT_SECRET}"),
            ("discovery", "http://idp.example/discovery"),
            ("discovery", "https://idp.example/discovery?x=1"),
            ("discovery", "https://idp.example:0/discovery"),
            ("discovery", "https://idp.example:invalid/discovery"),
            ("introspection_endpoint", "http://idp.example/introspect"),
            ("introspection_endpoint_auth_method", "none"),
            ("bearer_only", False),
            ("ssl_verify", False),
            ("required_scopes", []),
            ("use_jwks", "true"),
            ("use_jwks", 1),
            ("use_jwks", None),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                print()
                # Given
                self.routing["security_schemes"]["authentik"] = {**self.idp, field: value}

                # When
                with self.assertRaises(ValueError) as error:
                    self.generator.generate(self.routing, self.spec)
                # Then
                self.assertTrue(str(error.exception))

    def test_manual_oidc_overrides_are_rejected_for_declared_security(self) -> None:
        requirements: list[object] = [[{"authentik": []}], []]
        for security in requirements:
            for location in [self.routing, self.routing["routes"]["getCamera"]]:
                with self.subTest(security=security, location=location):
                    print()
                    # Given
                    self.operation["security"] = security
                    location["plugins"] = {"openid-connect": {"unauth_action": "pass"}}

                    # When
                    with self.assertRaises(ConfigError) as error:
                        self.generator.generate(self.routing, self.spec)

                    # Then
                    self.assertIn("conflicts with OpenAPI security", str(error.exception))
                    location.pop("plugins")

    def test_cli_output_preserves_idp_environment_placeholders(self) -> None:
        # Given
        self.routing["api"] = {"spec": "openapi.yaml"}
        self.idp["discovery"] = "${{AUTHENTIK_DISCOVERY}}"
        self.idp["introspection_endpoint"] = "${{AUTHENTIK_INTROSPECTION}}"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            routing, spec, output = root / "routing.yaml", root / "openapi.yaml", root / "apisix.yaml"
            routing.write_text(yaml.safe_dump(self.routing), encoding="utf-8")
            spec.write_text(yaml.safe_dump(self.spec), encoding="utf-8")

            # When
            self.generator.generate_file(routing, output)

            # Then
            content = output.read_text(encoding="utf-8")
            self.assertTrue(content.endswith("#END\n"))
            self.assertEqual(yaml.safe_load(content), self.generator.generate(self.routing, self.spec))
            self.assertIn("${{AUTHENTIK_CLIENT_SECRET}}", content)
            self.assertIn("${{AUTHENTIK_DISCOVERY}}", content)

    def test_duplicate_scopes_are_deduplicated(self) -> None:
        # Given
        self.operation["security"] = [{"authentik": ["cameras:read", "cameras:read"]}]

        # When
        result = self.generator.generate(self.routing, self.spec)

        # Then
        plugin = result["routes"][0]["plugins"]["openid-connect"]
        assert isinstance(plugin, dict)
        self.assertEqual(plugin["required_scopes"], ["cameras:read"])

    def test_jwks_mode_preserves_scopes_and_enforces_audience(self) -> None:
        # Given
        self.idp["use_jwks"] = True
        original = copy.deepcopy((self.routing, self.spec))

        # When
        result = self.generator.generate(self.routing, self.spec)

        # Then
        plugin = result["routes"][0]["plugins"]["openid-connect"]
        self.assertEqual(
            plugin,
            {
                "client_id": self.idp["client_id"],
                "client_secret": self.idp["client_secret"],
                "discovery": self.idp["discovery"],
                "bearer_only": True,
                "unauth_action": "deny",
                "ssl_verify": True,
                "required_scopes": ["cameras:read"],
                "cache_segment": "security:authentik",
                "use_jwks": True,
                "accept_none_alg": False,
                "accept_unsupported_alg": False,
                "claim_validator": {"audience": {"required": True, "match_with_client_id": True}},
            },
        )
        self.assertEqual((self.routing, self.spec), original)

    def test_omitted_use_jwks_defaults_to_jwks(self) -> None:
        # Given
        self.idp["use_jwks"] = True
        expected = self.generator.generate(self.routing, self.spec)
        self.idp.pop("use_jwks")

        # When
        result = self.generator.generate(self.routing, self.spec)

        # Then
        self.assertEqual(result, expected)

    def test_default_jwks_rejects_introspection_settings(self) -> None:
        # Given
        self.idp.pop("use_jwks")
        self.idp["introspection_endpoint"] = "https://idp.example/introspect"

        # When
        with self.assertRaises(ConfigError) as error:
            self.generator.generate(self.routing, self.spec)

        # Then
        self.assertIn("cannot combine use_jwks with introspection settings", str(error.exception))

    def test_jwks_rejects_introspection_settings(self) -> None:
        for field, value in [
            ("introspection_endpoint", "https://idp.example/introspect"),
            ("introspection_endpoint_auth_method", "client_secret_post"),
        ]:
            with self.subTest(field=field):
                print()
                # Given
                self.routing["security_schemes"]["authentik"] = {**self.idp, "use_jwks": True, field: value}

                # When
                with self.assertRaises(ConfigError) as error:
                    self.generator.generate(self.routing, self.spec)

                # Then
                self.assertIn("cannot combine use_jwks with introspection settings", str(error.exception))

    def test_jwks_uses_oidc_discovery_from_spec(self) -> None:
        # Given
        discovery = self.idp.pop("discovery")
        self.idp["use_jwks"] = True
        self.spec["components"]["securitySchemes"]["authentik"] = {
            "type": "openIdConnect",
            "openIdConnectUrl": discovery,
        }

        # When
        result = self.generator.generate(self.routing, self.spec)

        # Then
        plugin = result["routes"][0]["plugins"]["openid-connect"]
        assert isinstance(plugin, dict)
        self.assertTrue(plugin["use_jwks"])
        self.assertEqual(plugin["discovery"], discovery)
        self.assertEqual(plugin["required_scopes"], ["cameras:read"])

    def test_authentik_example_matches_generated_configuration(self) -> None:
        # Given
        examples = Path(__file__).resolve().parents[1] / "examples"
        routing = self.generator.load_document(examples / "authentik-routing.yaml")
        spec = self.generator.load_document(examples / "authentik-openapi.yaml")
        expected = self.generator.load_document(examples / "authentik-apisix.yaml")

        # When
        result = self.generator.generate(routing, spec)

        # Then
        self.assertEqual(result, expected)
        self.assertIn("openid-connect", result["routes"][0]["plugins"])
        self.assertNotIn("plugins", result["routes"][1])

    def test_invalid_security_preserves_existing_configuration(self) -> None:
        # Given
        self.routing["api"] = {"spec": "openapi.yaml"}
        self.operation["security"] = [{"unknown": []}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            routing, spec, output = root / "routing.yaml", root / "openapi.yaml", root / "apisix.yaml"
            routing.write_text(yaml.safe_dump(self.routing), encoding="utf-8")
            spec.write_text(yaml.safe_dump(self.spec), encoding="utf-8")
            output.write_text("existing configuration", encoding="utf-8")

            # When
            with self.assertRaises(ConfigError) as error:
                self.generator.generate_file(routing, output)

            # Then
            self.assertIn("unknown security scheme", str(error.exception))
            self.assertEqual(output.read_text(encoding="utf-8"), "existing configuration")
            self.assertEqual(
                sorted(path.name for path in root.iterdir()), ["apisix.yaml", "openapi.yaml", "routing.yaml"]
            )
