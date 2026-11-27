import tempfile
from pathlib import Path
from typing import Any
from unittest import TestCase
from unittest.mock import patch

import yaml
from context_logger import setup_logging

from config_generator import ConfigError, OpenapiConfigGenerator, OpenapiSecurityProvider, generate


class TestSecurityIntegration(TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        setup_logging("apisix-config-generator", "DEBUG", warn_on_overwrite=False)

    def setUp(self) -> None:
        print()
        self.generator = OpenapiConfigGenerator(OpenapiSecurityProvider())
        self.operation: dict[str, Any] = {"operationId": "getCamera", "security": [{"authentik": ["cameras:read"]}]}
        self.spec: dict[str, Any] = {
            "openapi": "3.1.0",
            "paths": {"/cameras/{cameraId}": {"get": self.operation}},
            "components": {
                "securitySchemes": {
                    "authentik": {
                        "type": "oauth2",
                        "flows": {
                            "clientCredentials": {
                                "tokenUrl": "https://idp.example/token",
                                "scopes": {"cameras:read": "Read", "cameras:write": "Write"},
                            }
                        },
                    },
                }
            },
        }
        self.idp: dict[str, Any] = {
            "client_id": "${{AUTHENTIK_CLIENT_ID}}",
            "client_secret": "${{AUTHENTIK_CLIENT_SECRET}}",
            "discovery": "https://idp.example/application/o/cameras/.well-known/openid-configuration",
        }
        self.routing: dict[str, Any] = {
            "api": {"spec": "openapi.yaml"},
            "routes": {"getCamera": {"service": "camera"}},
            "services": {"camera": {"url": "http://camera:8080"}},
            "security": {"authentik": self.idp},
        }

    def test_composition_preserves_plugins_and_separates_route_scopes(self) -> None:
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

        # Then
        self.assertEqual(result, generate(self.routing, self.spec))
        first, second = result["routes"][0]["plugins"], result["routes"][1]["plugins"]
        self.assertIn("cors", first)
        self.assertIn("cors", second)
        self.assertIn("limit-count", first)
        self.assertNotIn("limit-count", second)
        first_auth, second_auth = first["openid-connect"], second["openid-connect"]
        assert isinstance(first_auth, dict) and isinstance(second_auth, dict)
        self.assertEqual(first_auth["required_scopes"], ["cameras:read"])
        self.assertEqual(second_auth["required_scopes"], ["cameras:write"])

    def test_global_security_and_public_override(self) -> None:
        # Given
        self.spec["security"] = self.operation.pop("security")
        self.spec["paths"]["/health"] = {"get": {"operationId": "getHealth", "security": []}}
        self.routing["routes"]["getHealth"] = {"service": "camera"}

        # When
        result = self.generator.generate(self.routing, self.spec)

        # Then
        self.assertIn("openid-connect", result["routes"][0]["plugins"])
        self.assertNotIn("plugins", result["routes"][1])

    def test_manual_plugins_conflict_at_global_and_route_levels(self) -> None:
        for location in [self.routing, self.routing["routes"]["getCamera"]]:
            with self.subTest(location=location):
                print()
                # Given
                location["plugins"] = {"openid-connect": {"unauth_action": "pass"}}

                # When
                with self.assertRaises(ConfigError) as error:
                    self.generator.generate(self.routing, self.spec)

                # Then
                self.assertIn("conflicts with OpenAPI security", str(error.exception))
                location.pop("plugins")

    def test_security_errors_are_logged_and_reraised(self) -> None:
        # Given
        self.idp["client_secret"] = " "
        with patch("config_generator.generator.get_logger") as get_logger:
            generator = OpenapiConfigGenerator(OpenapiSecurityProvider())

            # When
            with self.assertRaises(ConfigError) as error:
                generator.generate(self.routing, self.spec)

            # Then
            get_logger.return_value.error.assert_called_once_with(
                "Configuration generation failed", error=str(error.exception)
            )
            self.assertIn("client_secret must be a nonempty string", str(error.exception))

    def test_file_output_preserves_idp_placeholders_in_both_modes(self) -> None:
        for use_jwks in [False, True]:
            with self.subTest(use_jwks=use_jwks), tempfile.TemporaryDirectory() as directory:
                print()
                # Given
                self.idp["use_jwks"] = use_jwks
                self.idp["discovery"] = "${{AUTHENTIK_DISCOVERY}}"
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

    def test_authentik_example_matches_generated_configuration(self) -> None:
        # Given
        examples = Path(__file__).resolve().parents[2] / "examples"
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
