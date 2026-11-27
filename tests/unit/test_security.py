import copy
import inspect
from typing import Any
from unittest import TestCase
from unittest.mock import patch

from config_generator import ConfigError, OpenapiSecurityProvider, SecurityProvider
from context_logger import setup_logging


class TestSecurity(TestCase):
    def test_security_logging_excludes_credentials(self) -> None:
        for use_jwks in [False, True]:
            with self.subTest(use_jwks=use_jwks):
                print()
                # Given
                self.idp["use_jwks"] = use_jwks
                with patch("config_generator.security.get_logger") as get_logger:
                    provider = OpenapiSecurityProvider()

                    # When
                    provider.generate(self.operation, self.routing, self.spec, "getCamera", {})

                    # Then
                    get_logger.assert_called_once_with("OpenapiSecurityProvider")
                    get_logger.return_value.debug.assert_called_once_with(
                        "Generating security plugins", operation_id="getCamera"
                    )
                    get_logger.return_value.info.assert_called_once_with(
                        "Security plugin generated",
                        operation_id="getCamera",
                        plugin="openid-connect",
                        validation_mode="jwks" if use_jwks else "introspection",
                        required_scopes=["cameras:read"],
                    )

    def test_public_operation_logging(self) -> None:
        # Given
        self.operation["security"] = []
        with patch("config_generator.security.get_logger") as get_logger:
            provider = OpenapiSecurityProvider()

            # When
            result = provider.generate(self.operation, self.routing, self.spec, "getCamera", {})

            # Then
            self.assertEqual(result, {})
            get_logger.return_value.debug.assert_any_call("No security plugin required", operation_id="getCamera")
            get_logger.return_value.info.assert_not_called()

    @classmethod
    def setUpClass(cls) -> None:
        setup_logging("apisix-config-generator", "DEBUG", warn_on_overwrite=False)

    def setUp(self) -> None:
        print()
        self.provider: SecurityProvider = OpenapiSecurityProvider()
        self.operation: dict[str, Any] = {"security": [{"authentik": ["cameras:read"]}]}
        self.scheme: dict[str, Any] = {
            "type": "oauth2",
            "flows": {"authorizationCode": {"scopes": {"cameras:read": "Read", "cameras:write": "Write"}}},
        }
        self.spec: dict[str, Any] = {"components": {"securitySchemes": {"authentik": self.scheme}}}
        self.idp: dict[str, Any] = {
            "use_jwks": False,
            "client_id": "${{AUTHENTIK_CLIENT_ID}}",
            "client_secret": "${{AUTHENTIK_CLIENT_SECRET}}",
            "discovery": "https://idp.example/application/o/cameras/.well-known/openid-configuration",
        }
        self.routing: dict[str, Any] = {"security_schemes": {"authentik": self.idp}}

    def test_oauth2_security_generates_introspection_plugin(self) -> None:
        # Given
        plugins: dict[str, object] = {"cors": {}}
        original = copy.deepcopy((self.operation, self.routing, self.spec, plugins))

        # When
        result = self.provider.generate(self.operation, self.routing, self.spec, "getCamera", plugins)

        # Then
        self.assertTrue(inspect.isabstract(SecurityProvider))
        self.assertEqual(
            result,
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
        self.assertEqual((self.operation, self.routing, self.spec, plugins), original)

    def test_global_security_is_inherited(self) -> None:
        # Given
        self.spec["security"] = self.operation.pop("security")

        # When
        result = self.provider.generate(self.operation, self.routing, self.spec, "getCamera", {})

        # Then
        plugin = result["openid-connect"]
        assert isinstance(plugin, dict)
        self.assertEqual(plugin["required_scopes"], ["cameras:read"])

    def test_operation_security_overrides_global_security(self) -> None:
        # Given
        self.spec["security"] = [{"authentik": ["cameras:write"]}]

        # When
        result = self.provider.generate(self.operation, self.routing, self.spec, "getCamera", {})

        # Then
        plugin = result["openid-connect"]
        assert isinstance(plugin, dict)
        self.assertEqual(plugin["required_scopes"], ["cameras:read"])

    def test_public_operations_return_no_security_plugins(self) -> None:
        requirements: list[object] = [[], [{}]]
        for security in requirements:
            with self.subTest(security=security):
                print()
                # Given
                self.spec["security"] = [{"authentik": ["cameras:write"]}]
                self.operation["security"] = security
                self.routing["security_schemes"] = {}

                # When
                result = self.provider.generate(self.operation, self.routing, self.spec, "getCamera", {})

                # Then
                self.assertEqual(result, {})

    def test_undeclared_security_does_not_change_manual_plugins(self) -> None:
        # Given
        self.operation.clear()
        plugins: dict[str, object] = {"openid-connect": {"bearer_only": True}}
        original = copy.deepcopy(plugins)

        # When
        result = self.provider.generate(self.operation, self.routing, self.spec, "getCamera", plugins)

        # Then
        self.assertEqual(result, {})
        self.assertEqual(plugins, original)

    def test_bearer_and_oidc_schemes(self) -> None:
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
                result = self.provider.generate(self.operation, self.routing, self.spec, "getCamera", {})

                # Then
                plugin = result["openid-connect"]
                assert isinstance(plugin, dict)
                self.assertTrue(plugin["bearer_only"])
                self.assertEqual(plugin["required_scopes"], [])

    def test_oidc_discovery_defaults_to_spec_in_both_modes(self) -> None:
        for use_jwks in [False, True]:
            with self.subTest(use_jwks=use_jwks):
                print()
                # Given
                discovery = self.idp.get("discovery", "${{AUTHENTIK_DISCOVERY}}")
                self.idp.pop("discovery", None)
                self.idp["use_jwks"] = use_jwks
                self.spec["components"]["securitySchemes"]["authentik"] = {
                    "type": "openIdConnect",
                    "openIdConnectUrl": discovery,
                }

                # When
                result = self.provider.generate(self.operation, self.routing, self.spec, "getCamera", {})

                # Then
                plugin = result["openid-connect"]
                assert isinstance(plugin, dict)
                self.assertEqual(plugin["discovery"], discovery)
                self.assertEqual(plugin.get("use_jwks", False), use_jwks)

    def test_introspection_settings_and_placeholders(self) -> None:
        # Given
        self.idp.update(
            discovery="${{AUTHENTIK_DISCOVERY}}",
            introspection_endpoint="${{AUTHENTIK_INTROSPECTION}}",
            introspection_endpoint_auth_method="client_secret_post",
        )

        # When
        result = self.provider.generate(self.operation, self.routing, self.spec, "getCamera", {})

        # Then
        plugin = result["openid-connect"]
        assert isinstance(plugin, dict)
        self.assertEqual(plugin["discovery"], "${{AUTHENTIK_DISCOVERY}}")
        self.assertEqual(plugin["introspection_endpoint"], "${{AUTHENTIK_INTROSPECTION}}")
        self.assertEqual(plugin["introspection_endpoint_auth_method"], "client_secret_post")
        self.assertEqual(plugin["client_secret"], "${{AUTHENTIK_CLIENT_SECRET}}")

    def test_jwks_is_default_and_enforces_audience_and_scopes(self) -> None:
        # Given
        self.idp.pop("use_jwks")
        original = copy.deepcopy((self.operation, self.routing, self.spec))

        # When
        result = self.provider.generate(self.operation, self.routing, self.spec, "getCamera", {})
        self.idp["use_jwks"] = True
        explicit = self.provider.generate(self.operation, self.routing, self.spec, "getCamera", {})
        self.idp.pop("use_jwks")

        # Then
        self.assertEqual(result, explicit)
        self.assertEqual(
            result["openid-connect"],
            {
                **self.idp,
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
        self.assertEqual((self.operation, self.routing, self.spec), original)

    def test_jwks_rejects_introspection_settings(self) -> None:
        for explicit in [False, True]:
            for field, value in [
                ("introspection_endpoint", "https://idp.example/introspect"),
                ("introspection_endpoint_auth_method", "client_secret_post"),
            ]:
                with self.subTest(explicit=explicit, field=field):
                    print()
                    # Given
                    settings = {key: item for key, item in self.idp.items() if key != "use_jwks"}
                    if explicit:
                        settings["use_jwks"] = True
                    settings[field] = value
                    self.routing["security_schemes"]["authentik"] = settings

                    # When
                    with self.assertRaises(ConfigError) as error:
                        self.provider.generate(self.operation, self.routing, self.spec, "getCamera", {})

                    # Then
                    self.assertIn("cannot combine use_jwks with introspection settings", str(error.exception))

    def test_invalid_security_requirements(self) -> None:
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
                    self.provider.generate(self.operation, self.routing, self.spec, "getCamera", {})

                # Then
                self.assertIn(message, str(error.exception))

    def test_invalid_security_schemes(self) -> None:
        for scheme, message in [
            ({"type": "apiKey"}, "only HTTP bearer"),
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
                    self.provider.generate(self.operation, self.routing, self.spec, "getCamera", {})

                # Then
                self.assertIn(message, str(error.exception))

    def test_missing_idp_configuration(self) -> None:
        # Given
        self.routing.clear()

        # When
        with self.assertRaises(ConfigError) as error:
            self.provider.generate(self.operation, self.routing, self.spec, "getCamera", {})

        # Then
        self.assertIn("requires IDP settings", str(error.exception))

    def test_invalid_idp_settings(self) -> None:
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
                    self.provider.generate(self.operation, self.routing, self.spec, "getCamera", {})

                # Then
                self.assertTrue(str(error.exception))

    def test_manual_oidc_conflicts(self) -> None:
        requirements: list[object] = [[{"authentik": []}], []]
        for security in requirements:
            with self.subTest(security=security):
                print()
                # Given
                self.operation["security"] = security
                plugins: dict[str, object] = {"openid-connect": {"unauth_action": "pass"}}

                # When
                with self.assertRaises(ConfigError) as error:
                    self.provider.generate(self.operation, self.routing, self.spec, "getCamera", plugins)

                # Then
                self.assertIn("conflicts with OpenAPI security", str(error.exception))

    def test_duplicate_scopes_and_independent_results(self) -> None:
        # Given
        self.operation["security"] = [{"authentik": ["cameras:read", "cameras:read"]}]
        other: dict[str, object] = {"security": [{"authentik": ["cameras:write"]}]}

        # When
        first = self.provider.generate(self.operation, self.routing, self.spec, "getCamera", {})
        second = self.provider.generate(other, self.routing, self.spec, "updateCamera", {})

        # Then
        first_auth, second_auth = first["openid-connect"], second["openid-connect"]
        assert isinstance(first_auth, dict) and isinstance(second_auth, dict)
        self.assertEqual(first_auth["required_scopes"], ["cameras:read"])
        self.assertEqual(second_auth["required_scopes"], ["cameras:write"])
        first_auth["required_scopes"].clear()
        self.assertEqual(second_auth["required_scopes"], ["cameras:write"])
