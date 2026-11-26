import contextlib
import copy
import io
import inspect
import os
import tempfile
import unittest
from pathlib import Path
from collections.abc import Callable
from typing import Any
from unittest.mock import patch
from urllib.error import URLError
from unittest import TestCase

import yaml

from config_generator import ConfigError, ConfigGenerator, OpenapiConfigGenerator, generate, load_document, main
from context_logger import setup_logging


class TestGenerator(TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        setup_logging("apisix-config-generator", "DEBUG", warn_on_overwrite=False)

    def setUp(self) -> None:
        print()
        self.generator: ConfigGenerator = OpenapiConfigGenerator()
        self.routing: dict[str, Any] = {
            "api": {"spec": "openapi.yaml"},
            "routes": {"getCamera": {"service": "camera"}},
            "services": {"camera": {"url": "http://camera-service:8080"}},
        }
        self.spec: dict[str, Any] = {
            "openapi": "3.1.0",
            "paths": {"/cameras/{cameraId}": {"get": {"operationId": "getCamera"}}},
        }

    def test_route_matches_operation_and_service(self) -> None:
        # Given
        routing, spec = self.routing, self.spec

        # When
        result = generate(routing, spec)

        # Then
        self.assertEqual(
            result,
            {
                "routes": [
                    {
                        "id": "getCamera",
                        "uri": "/cameras/:cameraId",
                        "methods": ["GET"],
                        "upstream": {
                            "type": "roundrobin",
                            "scheme": "http",
                            "nodes": {"camera-service:8080": 1},
                            "pass_host": "pass",
                        },
                    }
                ],
            },
        )

    def test_only_selected_operations_are_exposed(self) -> None:
        # Given
        self.spec["paths"]["/cameras"] = {"post": {"operationId": "createCamera"}}

        # When
        routes = generate(self.routing, self.spec)["routes"]

        # Then
        self.assertEqual(len(routes), 1)

    def test_nested_parameters_and_methods(self) -> None:
        # Given
        self.spec["paths"] = {
            "/cameras/{cameraId}/images/{imageId}": {
                "parameters": [],
                "delete": {"operationId": "getCamera"},
                "get": {"operationId": "notSelected"},
            },
        }

        # When
        routes = generate(self.routing, self.spec)["routes"]
        route = routes[0]

        # Then
        self.assertEqual(route["uri"], "/cameras/:cameraId/images/:imageId")
        self.assertEqual(route["methods"], ["DELETE"])
        self.assertEqual(len(routes), 1)

    def test_plugins_override_by_name_without_mutating_inputs(self) -> None:
        # Given
        self.routing["plugins"] = {"cors": {}, "limit-count": {"count": 10}}
        self.routing["routes"]["getCamera"]["plugins"] = {"limit-count": {"count": 2}}
        original = copy.deepcopy(self.routing)

        # When
        plugins = generate(self.routing, self.spec)["routes"][0]["plugins"]

        # Then
        self.assertEqual(plugins, {"cors": {}, "limit-count": {"count": 2}})

        # When
        cors = plugins["cors"]
        assert isinstance(cors, dict)
        cors["allow_origins"] = "*"

        # Then
        self.assertEqual(self.routing, original)

    def test_upstream_origins(self) -> None:
        for url, node, scheme in [
            ("https://camera.example", "camera.example:443", "https"),
            ("http://camera.example/", "camera.example:80", "http"),
            ("http://[::1]:8080", "[::1]:8080", "http"),
        ]:
            with self.subTest(url=url):
                print()
                # Given
                self.routing["services"]["camera"]["url"] = url

                # When
                result = generate(self.routing, self.spec)["routes"][0]["upstream"]

                # Then
                self.assertEqual(result["nodes"], {node: 1})
                self.assertEqual(result["scheme"], scheme)

    def test_invalid_inputs(self) -> None:
        # Given
        changes: list[tuple[Callable[[dict[str, Any], dict[str, Any]], None], str]] = [
            (lambda r, s: r["routes"].update(unknown={}), "Unknown operationId"),
            (lambda r, s: r["services"].clear(), "unknown service"),
            (
                lambda r, s: s["paths"].update({"/other": {"get": {"operationId": "getCamera"}}}),
                "Duplicate operationId",
            ),
            (
                lambda r, s: s.update(paths={"/cameras/{id}.json": {"get": {"operationId": "getCamera"}}}),
                "Unsupported path segment",
            ),
            (lambda r, s: s["paths"].update({"/other": {"$ref": "external.yaml"}}), "Path item"),
            (lambda r, s: r.update(routes={}), "must not be empty"),
            (lambda r, s: s.update(openapi="2.0"), "OpenAPI 3.x"),
        ]
        for change, message in changes:
            with self.subTest(message=message):
                print()
                routing, spec = copy.deepcopy(self.routing), copy.deepcopy(self.spec)
                change(routing, spec)

                # When
                with self.assertRaises(ConfigError) as error:
                    generate(routing, spec)

                # Then
                self.assertRegex(str(error.exception), message)

    def test_upstream_environment_placeholders_are_not_resolved(self) -> None:
        for environment in [{}, {"CAMERA_SERVICE_ADDR": "camera-service:8080"}]:
            for scheme in ["http", "https"]:
                for suffix in ["", "/"]:
                    with self.subTest(environment=environment, scheme=scheme, suffix=suffix):
                        print()
                        # Given
                        self.routing["services"]["camera"]["url"] = f"{scheme}://${{{{CAMERA_SERVICE_ADDR}}}}{suffix}"
                        with patch.dict(os.environ, environment, clear=True):

                            # When
                            result = generate(self.routing, self.spec)["routes"][0]["upstream"]

                        # Then
                        self.assertEqual(
                            result,
                            {
                                "type": "roundrobin",
                                "scheme": scheme,
                                "nodes": {"${{CAMERA_SERVICE_ADDR}}": 1},
                                "pass_host": "pass",
                            },
                        )

    def test_reject_invalid_environment_urls(self) -> None:
        for url in [
            "${{CAMERA_SERVICE_ADDR}}",
            "ftp://${{CAMERA_SERVICE_ADDR}}",
            "http://${CAMERA_SERVICE_ADDR}",
            "http://${{}}",
            "http://${{1ADDR}}",
            "http://${{CAMERA-SERVICE-ADDR}}",
            "http://${{ CAMERA_SERVICE_ADDR }}",
            "http://${{CAMERA_SERVICE_ADDR}",
            "http://${{CAMERA_SERVICE_ADDR}}:8080",
            "http://${{CAMERA_SERVICE_ADDR}}/api",
            "http://${{CAMERA_SERVICE_ADDR}}?x=1",
            "http://${{CAMERA_SERVICE_ADDR}}#part",
            "http://user@${{CAMERA_SERVICE_ADDR}}",
        ]:
            with self.subTest(url=url):
                print()
                # Given
                self.routing["services"]["camera"]["url"] = url

                # When
                with self.assertRaises(ConfigError) as error:
                    generate(self.routing, self.spec)

                # Then
                self.assertIn("environment URL", str(error.exception))

    def test_ambiguous_parameter_routes_fail(self) -> None:
        # Given
        self.spec["paths"]["/cameras/{id}"] = {"get": {"operationId": "other"}}
        self.routing["routes"]["other"] = {"service": "camera"}

        # When
        with self.assertRaises(ConfigError) as error:
            generate(self.routing, self.spec)

            # Then
        self.assertIn("Duplicate route match", str(error.exception))

    def test_reject_invalid_upstream_urls(self) -> None:
        for url in [
            "ftp://camera",
            "http://camera/api",
            "http://user:pass@camera",
            "http://camera?x=1",
            "http://camera#part",
            "http://camera:invalid",
        ]:
            with self.subTest(url=url):
                print()
                # Given
                self.routing["services"]["camera"]["url"] = url

                # When
                with self.assertRaises(ValueError) as error:
                    generate(self.routing, self.spec)

                # Then
                self.assertTrue(str(error.exception))

    def test_remote_yaml_and_json(self) -> None:
        for content in [
            b"openapi: 3.1.0\npaths: {}\n",
            b'{"openapi": "3.1.0", "paths": {}}',
        ]:
            with self.subTest(content=content):
                print()
                # Given
                with patch("config_generator.generator.urlopen") as fetch:
                    fetch.return_value.__enter__.return_value.read.return_value = content

                    # When
                    result = load_document("https://example.com/openapi.yaml")

                    # Then
                    self.assertEqual(result, {"openapi": "3.1.0", "paths": {}})
                    fetch.assert_called_once_with("https://example.com/openapi.yaml", timeout=30)

    def test_cli_writes_complete_file_and_preserves_output_on_error(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            routing = root / "routing.yaml"
            spec = root / "openapi.yaml"
            output = root / "config" / "apisix.yaml"
            routing.write_text(yaml.safe_dump(self.routing), encoding="utf-8")
            spec.write_text(yaml.safe_dump(self.spec), encoding="utf-8")
            args = ["--routing", str(routing), "--output", str(output)]
            with contextlib.redirect_stdout(io.StringIO()):

                # When
                result = main(args)

                # Then
            self.assertEqual(result, 0)
            content = output.read_text(encoding="utf-8")
            self.assertTrue(content.endswith("#END\n"))
            self.assertEqual(yaml.safe_load(content), generate(self.routing, self.spec))
            # Given
            spec.write_text("invalid: [", encoding="utf-8")

            # When
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(args)

                # Then
            self.assertEqual(error.exception.code, 1)
            self.assertEqual(output.read_text(encoding="utf-8"), content)
            self.assertFalse(output.with_name("apisix.yaml.tmp").exists())

    def test_cli_preserves_environment_placeholder_in_yaml(self) -> None:
        # Given
        self.routing["services"]["camera"]["url"] = "http://${{CAMERA_SERVICE_ADDR}}"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            routing = root / "routing.yaml"
            spec = root / "openapi.yaml"
            output = root / "apisix.yaml"
            routing.write_text(yaml.safe_dump(self.routing), encoding="utf-8")
            spec.write_text(yaml.safe_dump(self.spec), encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()):

                # When
                result = main(["--routing", str(routing), "--output", str(output)])

                # Then
            self.assertEqual(result, 0)
            content = output.read_text(encoding="utf-8")
            self.assertTrue(content.endswith("#END\n"))
            self.assertIn("${{CAMERA_SERVICE_ADDR}}", content)
            upstream = yaml.safe_load(content)["routes"][0]["upstream"]
            self.assertEqual(upstream["nodes"], {"${{CAMERA_SERVICE_ADDR}}": 1})
            self.assertEqual(upstream["scheme"], "http")

    def test_generator_interface_and_compatibility(self) -> None:
        # Given
        generator = self.generator

        # When
        result = generator.generate(self.routing, self.spec)
        expected = generate(self.routing, self.spec)

        # Then
        self.assertTrue(inspect.isabstract(ConfigGenerator))
        self.assertIsInstance(self.generator, ConfigGenerator)
        self.assertEqual(result, expected)

    def test_generator_can_be_reused_without_mutating_inputs(self) -> None:
        # Given
        original = copy.deepcopy(self.routing), copy.deepcopy(self.spec)
        first = self.generator.generate(self.routing, self.spec)
        first["routes"][0]["upstream"]["nodes"].clear()

        # When
        second = self.generator.generate(self.routing, self.spec)

        # Then
        self.assertEqual(second["routes"][0]["upstream"]["nodes"], {"camera-service:8080": 1})
        self.assertEqual((self.routing, self.spec), original)

    def test_document_mapping_validation(self) -> None:
        for content in ["- not a mapping\n", "1: invalid key\n"]:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                print()
                # Given
                source = Path(directory) / "routing.yaml"
                source.write_text(content, encoding="utf-8")

                # When
                with self.assertRaises(ConfigError) as error:
                    self.generator.load_document(source)

                # Then
                self.assertIn(str(source), str(error.exception))

    def test_cli_logs_errors_and_preserves_existing_output(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as directory:
            routing = Path(directory) / "missing.yaml"
            output = Path(directory) / "apisix.yaml"
            output.write_text("existing configuration", encoding="utf-8")
            with patch("config_generator.generator.get_logger") as get_logger:

                # When
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    main(["--routing", str(routing), "--output", str(output)])

                # Then
                self.assertEqual(error.exception.code, 1)
                get_logger.return_value.error.assert_called_once()
                self.assertEqual(get_logger.return_value.error.call_args.args, ("Document loading failed",))
                self.assertEqual(get_logger.return_value.error.call_args.kwargs["source"], str(routing))
            self.assertEqual(output.read_text(encoding="utf-8"), "existing configuration")

    def test_library_errors_are_logged_and_reraised(self) -> None:
        # Given
        config = self.generator.generate(self.routing, self.spec)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            routing = root / "routing.yaml"
            output = root / "apisix.yaml"
            self.routing["api"]["spec"] = ""
            routing.write_text(yaml.safe_dump(self.routing), encoding="utf-8")
            with patch("config_generator.generator.get_logger") as get_logger:
                generator = OpenapiConfigGenerator()
                logger = get_logger.return_value
                failures: list[tuple[Callable[[], object], type[Exception], str]] = [
                    (lambda: generator.generate({}, self.spec), ConfigError, "Configuration generation failed"),
                    (lambda: generator.load_document(root / "missing.yaml"), OSError, "Document loading failed"),
                    (lambda: generator.generate_file(routing, output), ConfigError, "Configuration generation failed"),
                ]
                for action, exception_type, message in failures:
                    with self.subTest(message=message):
                        print()
                        logger.reset_mock()

                        # When
                        with self.assertRaises(exception_type) as error:
                            action()

                        # Then
                        logger.error.assert_called_once()
                        self.assertEqual(logger.error.call_args.args, (message,))
                        self.assertEqual(logger.error.call_args.kwargs["error"], str(error.exception))
                # Given
                logger.reset_mock()
                failure = OSError("disk full")
                with patch("config_generator.generator.tempfile.NamedTemporaryFile", side_effect=failure):

                    # When
                    with self.assertRaises(OSError) as write_error:
                        generator.write_config(config, output)

                # Then
                self.assertIs(write_error.exception, failure)
                logger.error.assert_called_once_with(
                    "Configuration writing failed", error="disk full", output=str(output)
                )

    def test_document_parse_and_network_errors_are_logged(self) -> None:
        for failure in [yaml.YAMLError("invalid YAML"), URLError("unavailable"), UnicodeError("invalid encoding")]:
            with self.subTest(failure=failure):
                print()
                # Given
                with patch("config_generator.generator.get_logger") as get_logger:
                    generator = OpenapiConfigGenerator()
                    with patch("config_generator.generator.urlopen", side_effect=failure):

                        # When
                        with self.assertRaises(type(failure)) as error:
                            generator.load_document("https://example.com/openapi.yaml")

                    # Then
                    self.assertIs(error.exception, failure)
                    get_logger.return_value.error.assert_called_once_with(
                        "Document loading failed", error=str(failure), source="https://example.com/openapi.yaml"
                    )

    def test_invalid_route_ids(self) -> None:
        for operation_id in ["", "camera.get", "a" * 65]:
            with self.subTest(operation_id=operation_id):
                print()
                # Given
                self.routing["routes"] = {operation_id: {"service": "camera"}}

                # When
                with self.assertRaises(ConfigError) as error:
                    self.generator.generate(self.routing, self.spec)

                # Then
                self.assertIn("must be a valid APISIX ID", str(error.exception))

    def test_invalid_openapi_paths_and_operations(self) -> None:
        for paths, message in [
            ({"cameras": {"get": {"operationId": "getCamera"}}}, "Invalid OpenAPI path"),
            ({"/cameras": {"get": {"operationId": ""}}}, "Invalid operationId"),
            ({"/cameras": {"get": {"operationId": 42}}}, "Invalid operationId"),
            ({"/cameras": {"get": []}}, "GET /cameras must be a mapping"),
            ({"/cameras": []}, "Path /cameras must be a mapping"),
        ]:
            with self.subTest(paths=paths):
                print()
                # Given
                self.spec["paths"] = paths

                # When
                with self.assertRaises(ConfigError) as error:
                    self.generator.generate(self.routing, self.spec)

                # Then
                self.assertRegex(str(error.exception), message)

    def test_operations_without_ids_are_ignored(self) -> None:
        # Given
        self.spec["paths"]["/unselected"] = {"get": {"responses": {}}}

        # When
        routes = self.generator.generate(self.routing, self.spec)["routes"]

        # Then
        self.assertEqual([route["id"] for route in routes], ["getCamera"])

    def test_same_path_with_distinct_methods_is_allowed(self) -> None:
        # Given
        self.spec["paths"]["/cameras/{cameraId}"]["delete"] = {"operationId": "deleteCamera"}
        self.routing["routes"]["deleteCamera"] = {"service": "camera"}

        # When
        routes = self.generator.generate(self.routing, self.spec)["routes"]

        # Then
        self.assertEqual([route["methods"] for route in routes], [["GET"], ["DELETE"]])
        self.assertEqual([route["uri"] for route in routes], ["/cameras/:cameraId"] * 2)

    def test_missing_or_invalid_service_url(self) -> None:
        for service in [{}, {"url": None}, {"url": 42}]:
            with self.subTest(service=service):
                print()
                # Given
                self.routing["services"]["camera"] = service

                # When
                with self.assertRaises(ConfigError) as error:
                    self.generator.generate(self.routing, self.spec)

                # Then
                self.assertIn("Service camera requires a URL", str(error.exception))

    def test_reject_invalid_origin_addresses(self) -> None:
        for url in [
            "http://",
            "http://user:password@camera",
            "http://camera:0",
            "http://camera:65536",
            "http://camera service:8080",
            "http://camera:\t8080",
        ]:
            with self.subTest(url=url):
                print()
                # Given
                self.routing["services"]["camera"]["url"] = url

                # When
                with self.assertRaises(ValueError) as error:
                    self.generator.generate(self.routing, self.spec)

                # Then
                self.assertTrue(str(error.exception))

    def test_generate_file_rejects_missing_or_invalid_spec_source(self) -> None:
        for source in [None, "", 42]:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                print()
                # Given
                root = Path(directory)
                routing = root / "routing.yaml"
                output = root / "apisix.yaml"
                self.routing["api"]["spec"] = source
                routing.write_text(yaml.safe_dump(self.routing), encoding="utf-8")
                output.write_text("existing configuration", encoding="utf-8")

                # When
                with self.assertRaises(ConfigError) as error:
                    self.generator.generate_file(routing, output)

                # Then
                self.assertIn("api.spec must be a local path or HTTP", str(error.exception))
                self.assertEqual(output.read_text(encoding="utf-8"), "existing configuration")
                self.assertEqual(sorted(path.name for path in root.iterdir()), ["apisix.yaml", "routing.yaml"])

    def test_generate_file_loads_remote_spec(self) -> None:
        for source in ["http://example.com/openapi.yaml", "https://example.com/openapi.yaml"]:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                print()
                # Given
                root = Path(directory)
                routing = root / "routing.yaml"
                output = root / "config" / "apisix.yaml"
                self.routing["api"]["spec"] = source
                routing.write_text(yaml.safe_dump(self.routing), encoding="utf-8")
                with patch("config_generator.generator.urlopen") as fetch:
                    fetch.return_value.__enter__.return_value.read.return_value = (
                        "\ufeff" + yaml.safe_dump(self.spec)
                    ).encode("utf-8")

                    # When
                    self.generator.generate_file(routing, output)

                    # Then
                    fetch.assert_called_once_with(source, timeout=30)
                content = output.read_text(encoding="utf-8")
                self.assertTrue(content.endswith("#END\n"))
                self.assertEqual(yaml.safe_load(content), self.generator.generate(self.routing, self.spec))

    def test_load_local_document_with_bom_and_string_path(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "openapi.yaml"
            source.write_text("\ufeff" + yaml.safe_dump(self.spec), encoding="utf-8")

            # When
            result = self.generator.load_document(str(source))

            # Then
            self.assertEqual(result, self.spec)

    def test_remote_failure_preserves_existing_output(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            routing = root / "routing.yaml"
            output = root / "apisix.yaml"
            self.routing["api"]["spec"] = "https://example.com/openapi.yaml"
            routing.write_text(yaml.safe_dump(self.routing), encoding="utf-8")
            output.write_text("existing configuration", encoding="utf-8")
            with patch("config_generator.generator.urlopen", side_effect=URLError("unavailable")):

                # When
                with self.assertRaisesRegex(URLError, "unavailable"):
                    self.generator.generate_file(routing, output)

                # Then
            self.assertEqual(output.read_text(encoding="utf-8"), "existing configuration")
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["apisix.yaml", "routing.yaml"])

    def test_temporary_file_creation_failure_preserves_existing_output(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "apisix.yaml"
            output.write_text("existing configuration", encoding="utf-8")
            config = self.generator.generate(self.routing, self.spec)
            with patch("config_generator.generator.tempfile.NamedTemporaryFile", side_effect=OSError("disk full")):

                # When
                with self.assertRaisesRegex(OSError, "disk full"):
                    self.generator.write_config(config, output)

                # Then
            self.assertEqual(output.read_text(encoding="utf-8"), "existing configuration")
            self.assertEqual(list(output.parent.iterdir()), [output])

    def test_atomic_replace_failure_cleans_up_temporary_file(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "apisix.yaml"
            output.write_text("existing configuration", encoding="utf-8")
            config = self.generator.generate(self.routing, self.spec)
            with patch.object(Path, "replace", side_effect=PermissionError("read only")) as replace:

                # When
                with self.assertRaisesRegex(PermissionError, "read only"):
                    self.generator.write_config(config, output)

                # Then
                replace.assert_called_once_with(output)
            self.assertEqual(output.read_text(encoding="utf-8"), "existing configuration")
            self.assertEqual(list(output.parent.iterdir()), [output])


if __name__ == "__main__":
    unittest.main()
