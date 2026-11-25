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

import yaml

from config_generator import ConfigError, ConfigGenerator, OpenapiConfigGenerator, generate, load_document, main
from context_logger import setup_logging


class GeneratorTests(unittest.TestCase):
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
        self.assertEqual(generate(self.routing, self.spec), {
            "routes": [{
                "id": "getCamera",
                "uri": "/cameras/:cameraId",
                "methods": ["GET"],
                "upstream": {
                    "type": "roundrobin",
                    "scheme": "http",
                    "nodes": {"camera-service:8080": 1},
                    "pass_host": "pass",
                },
            }],
        })

    def test_only_selected_operations_are_exposed(self) -> None:
        self.spec["paths"]["/cameras"] = {"post": {"operationId": "createCamera"}}
        self.assertEqual(len(generate(self.routing, self.spec)["routes"]), 1)

    def test_nested_parameters_and_methods(self) -> None:
        self.spec["paths"] = {
            "/cameras/{cameraId}/images/{imageId}": {
                "parameters": [],
                "delete": {"operationId": "getCamera"},
                "get": {"operationId": "notSelected"},
            },
        }
        route = generate(self.routing, self.spec)["routes"][0]
        self.assertEqual(route["uri"], "/cameras/:cameraId/images/:imageId")
        self.assertEqual(route["methods"], ["DELETE"])
        self.assertEqual(len(generate(self.routing, self.spec)["routes"]), 1)

    def test_plugins_override_by_name_without_mutating_inputs(self) -> None:
        self.routing["plugins"] = {"cors": {}, "limit-count": {"count": 10}}
        self.routing["routes"]["getCamera"]["plugins"] = {"limit-count": {"count": 2}}
        original = copy.deepcopy(self.routing)
        plugins = generate(self.routing, self.spec)["routes"][0]["plugins"]
        self.assertEqual(plugins, {"cors": {}, "limit-count": {"count": 2}})
        cors = plugins["cors"]
        assert isinstance(cors, dict)
        cors["allow_origins"] = "*"
        self.assertEqual(self.routing, original)

    def test_upstream_origins(self) -> None:
        for url, node, scheme in [
            ("https://camera.example", "camera.example:443", "https"),
            ("http://camera.example/", "camera.example:80", "http"),
            ("http://[::1]:8080", "[::1]:8080", "http"),
        ]:
            with self.subTest(url=url):
                self.routing["services"]["camera"]["url"] = url
                result = generate(self.routing, self.spec)["routes"][0]["upstream"]
                self.assertEqual(result["nodes"], {node: 1})
                self.assertEqual(result["scheme"], scheme)

    def test_invalid_inputs(self) -> None:
        changes: list[tuple[Callable[[dict[str, Any], dict[str, Any]], None], str]] = [
            (lambda r, s: r["routes"].update(unknown={}), "Unknown operationId"),
            (lambda r, s: r["services"].clear(), "unknown service"),
            (lambda r, s: s["paths"].update(
                {"/other": {"get": {"operationId": "getCamera"}}}
            ), "Duplicate operationId"),
            (lambda r, s: s.update(paths={"/cameras/{id}.json": {
                "get": {"operationId": "getCamera"}
            }}), "Unsupported path segment"),
            (lambda r, s: s["paths"].update({"/other": {"$ref": "external.yaml"}}),
             "Path item"),
            (lambda r, s: r.update(routes={}), "must not be empty"),
            (lambda r, s: s.update(openapi="2.0"), "OpenAPI 3.x"),
        ]
        for change, message in changes:
            with self.subTest(message=message):
                routing, spec = copy.deepcopy(self.routing), copy.deepcopy(self.spec)
                change(routing, spec)
                with self.assertRaisesRegex(ConfigError, message):
                    generate(routing, spec)

    def test_upstream_environment_placeholders_are_not_resolved(self) -> None:
        for environment in [{}, {"CAMERA_SERVICE_ADDR": "camera-service:8080"}]:
            for scheme in ["http", "https"]:
                for suffix in ["", "/"]:
                    with self.subTest(environment=environment, scheme=scheme, suffix=suffix):
                        self.routing["services"]["camera"]["url"] = (
                            f"{scheme}://${{{{CAMERA_SERVICE_ADDR}}}}{suffix}"
                        )
                        with patch.dict(os.environ, environment, clear=True):
                            result = generate(self.routing, self.spec)["routes"][0]["upstream"]
                        self.assertEqual(result, {
                            "type": "roundrobin",
                            "scheme": scheme,
                            "nodes": {"${{CAMERA_SERVICE_ADDR}}": 1},
                            "pass_host": "pass",
                        })

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
                self.routing["services"]["camera"]["url"] = url
                with self.assertRaisesRegex(ConfigError, "environment URL"):
                    generate(self.routing, self.spec)

    def test_ambiguous_parameter_routes_fail(self) -> None:
        self.spec["paths"]["/cameras/{id}"] = {"get": {"operationId": "other"}}
        self.routing["routes"]["other"] = {"service": "camera"}
        with self.assertRaisesRegex(ConfigError, "Duplicate route match"):
            generate(self.routing, self.spec)

    def test_reject_invalid_upstream_urls(self) -> None:
        for url in [
            "ftp://camera", "http://camera/api", "http://user:pass@camera",
            "http://camera?x=1", "http://camera#part", "http://camera:invalid",
        ]:
            with self.subTest(url=url):
                self.routing["services"]["camera"]["url"] = url
                with self.assertRaises(ValueError):
                    generate(self.routing, self.spec)

    def test_remote_yaml_and_json(self) -> None:
        for content in [
            b'openapi: 3.1.0\npaths: {}\n',
            b'{"openapi": "3.1.0", "paths": {}}',
        ]:
            with self.subTest(content=content):
                with patch("config_generator.generator.urlopen") as fetch:
                    fetch.return_value.__enter__.return_value.read.return_value = content
                    result = load_document("https://example.com/openapi.yaml")
                    self.assertEqual(result, {"openapi": "3.1.0", "paths": {}})
                    fetch.assert_called_once_with(
                        "https://example.com/openapi.yaml", timeout=30
                    )

    def test_cli_writes_complete_file_and_preserves_output_on_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            routing = root / "routing.yaml"
            spec = root / "openapi.yaml"
            output = root / "config" / "apisix.yaml"
            routing.write_text(yaml.safe_dump(self.routing), encoding="utf-8")
            spec.write_text(yaml.safe_dump(self.spec), encoding="utf-8")
            args = ["--routing", str(routing), "--output", str(output)]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)
            content = output.read_text(encoding="utf-8")
            self.assertTrue(content.endswith("#END\n"))
            self.assertEqual(yaml.safe_load(content), generate(self.routing, self.spec))
            spec.write_text("invalid: [", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(args)
            self.assertEqual(error.exception.code, 1)
            self.assertEqual(output.read_text(encoding="utf-8"), content)
            self.assertFalse(output.with_name("apisix.yaml.tmp").exists())

    def test_cli_preserves_environment_placeholder_in_yaml(self) -> None:
        self.routing["services"]["camera"]["url"] = "http://${{CAMERA_SERVICE_ADDR}}"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            routing = root / "routing.yaml"
            spec = root / "openapi.yaml"
            output = root / "apisix.yaml"
            routing.write_text(yaml.safe_dump(self.routing), encoding="utf-8")
            spec.write_text(yaml.safe_dump(self.spec), encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--routing", str(routing), "--output", str(output)]), 0)
            content = output.read_text(encoding="utf-8")
            self.assertTrue(content.endswith("#END\n"))
            self.assertIn("${{CAMERA_SERVICE_ADDR}}", content)
            upstream = yaml.safe_load(content)["routes"][0]["upstream"]
            self.assertEqual(upstream["nodes"], {"${{CAMERA_SERVICE_ADDR}}": 1})
            self.assertEqual(upstream["scheme"], "http")

    def test_generator_interface_and_compatibility(self) -> None:
        self.assertTrue(inspect.isabstract(ConfigGenerator))
        self.assertIsInstance(self.generator, ConfigGenerator)
        self.assertEqual(self.generator.generate(self.routing, self.spec), generate(self.routing, self.spec))

    def test_generator_can_be_reused_without_mutating_inputs(self) -> None:
        original = copy.deepcopy(self.routing), copy.deepcopy(self.spec)
        first = self.generator.generate(self.routing, self.spec)
        first["routes"][0]["upstream"]["nodes"].clear()
        second = self.generator.generate(self.routing, self.spec)
        self.assertEqual(second["routes"][0]["upstream"]["nodes"], {"camera-service:8080": 1})
        self.assertEqual((self.routing, self.spec), original)

    def test_document_mapping_validation(self) -> None:
        for content in ["- not a mapping\n", "1: invalid key\n"]:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "routing.yaml"
                source.write_text(content, encoding="utf-8")
                with self.assertRaises(ConfigError):
                    self.generator.load_document(source)

    def test_generation_uses_context_logging(self) -> None:
        with patch("config_generator.generator.get_logger") as get_logger:
            generator = OpenapiConfigGenerator()
            generator.generate(self.routing, self.spec)
            get_logger.assert_called_once_with("OpenapiConfigGenerator")
            get_logger.return_value.info.assert_called_once_with("Configuration generated", route_count=1)
            get_logger.return_value.debug.assert_called_once_with(
                "Route generated", operation_id="getCamera", method="GET", uri="/cameras/:cameraId", service="camera"
            )

    def test_cli_logs_errors_and_preserves_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            routing = Path(directory) / "missing.yaml"
            output = Path(directory) / "apisix.yaml"
            output.write_text("existing configuration", encoding="utf-8")
            with patch("config_generator.generator.get_logger") as get_logger:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    main(["--routing", str(routing), "--output", str(output)])
                self.assertEqual(error.exception.code, 1)
                get_logger.return_value.error.assert_called_once()
                self.assertEqual(get_logger.return_value.error.call_args.args, ("Configuration generation failed",))
                self.assertEqual(get_logger.return_value.error.call_args.kwargs["routing"], str(routing))
            self.assertEqual(output.read_text(encoding="utf-8"), "existing configuration")

    def test_invalid_route_ids(self) -> None:
        for operation_id in ["", "camera.get", "a" * 65]:
            with self.subTest(operation_id=operation_id):
                self.routing["routes"] = {operation_id: {"service": "camera"}}
                with self.assertRaisesRegex(ConfigError, "must be a valid APISIX ID"):
                    self.generator.generate(self.routing, self.spec)

    def test_invalid_openapi_paths_and_operations(self) -> None:
        for paths, message in [
            ({"cameras": {"get": {"operationId": "getCamera"}}}, "Invalid OpenAPI path"),
            ({"/cameras": {"get": {"operationId": ""}}}, "Invalid operationId"),
            ({"/cameras": {"get": {"operationId": 42}}}, "Invalid operationId"),
            ({"/cameras": {"get": []}}, "GET /cameras must be a mapping"),
            ({"/cameras": []}, "Path /cameras must be a mapping"),
        ]:
            with self.subTest(paths=paths):
                self.spec["paths"] = paths
                with self.assertRaisesRegex(ConfigError, message):
                    self.generator.generate(self.routing, self.spec)

    def test_operations_without_ids_are_ignored(self) -> None:
        self.spec["paths"]["/unselected"] = {"get": {"responses": {}}}
        routes = self.generator.generate(self.routing, self.spec)["routes"]
        self.assertEqual([route["id"] for route in routes], ["getCamera"])

    def test_same_path_with_distinct_methods_is_allowed(self) -> None:
        self.spec["paths"]["/cameras/{cameraId}"]["delete"] = {"operationId": "deleteCamera"}
        self.routing["routes"]["deleteCamera"] = {"service": "camera"}
        routes = self.generator.generate(self.routing, self.spec)["routes"]
        self.assertEqual([route["methods"] for route in routes], [["GET"], ["DELETE"]])
        self.assertEqual([route["uri"] for route in routes], ["/cameras/:cameraId"] * 2)

    def test_missing_or_invalid_service_url(self) -> None:
        for service in [{}, {"url": None}, {"url": 42}]:
            with self.subTest(service=service):
                self.routing["services"]["camera"] = service
                with self.assertRaisesRegex(ConfigError, "Service camera requires a URL"):
                    self.generator.generate(self.routing, self.spec)

    def test_reject_invalid_origin_addresses(self) -> None:
        for url in [
            "http://", "http://user:password@camera", "http://camera:0",
            "http://camera:65536", "http://camera service:8080", "http://camera:\t8080",
        ]:
            with self.subTest(url=url):
                self.routing["services"]["camera"]["url"] = url
                with self.assertRaises(ValueError):
                    self.generator.generate(self.routing, self.spec)

    def test_generate_file_rejects_missing_or_invalid_spec_source(self) -> None:
        for source in [None, "", 42]:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                routing = root / "routing.yaml"
                output = root / "apisix.yaml"
                self.routing["api"]["spec"] = source
                routing.write_text(yaml.safe_dump(self.routing), encoding="utf-8")
                output.write_text("existing configuration", encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, "api.spec must be a local path or HTTP"):
                    self.generator.generate_file(routing, output)
                self.assertEqual(output.read_text(encoding="utf-8"), "existing configuration")
                self.assertEqual(sorted(path.name for path in root.iterdir()), ["apisix.yaml", "routing.yaml"])

    def test_generate_file_loads_remote_spec(self) -> None:
        for source in ["http://example.com/openapi.yaml", "https://example.com/openapi.yaml"]:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                routing = root / "routing.yaml"
                output = root / "config" / "apisix.yaml"
                self.routing["api"]["spec"] = source
                routing.write_text(yaml.safe_dump(self.routing), encoding="utf-8")
                with patch("config_generator.generator.urlopen") as fetch:
                    fetch.return_value.__enter__.return_value.read.return_value = (
                        "\ufeff" + yaml.safe_dump(self.spec)
                    ).encode("utf-8")
                    self.generator.generate_file(routing, output)
                    fetch.assert_called_once_with(source, timeout=30)
                content = output.read_text(encoding="utf-8")
                self.assertTrue(content.endswith("#END\n"))
                self.assertEqual(yaml.safe_load(content), self.generator.generate(self.routing, self.spec))

    def test_load_local_document_with_bom_and_string_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "openapi.yaml"
            source.write_text("\ufeff" + yaml.safe_dump(self.spec), encoding="utf-8")
            self.assertEqual(self.generator.load_document(str(source)), self.spec)

    def test_remote_failure_preserves_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            routing = root / "routing.yaml"
            output = root / "apisix.yaml"
            self.routing["api"]["spec"] = "https://example.com/openapi.yaml"
            routing.write_text(yaml.safe_dump(self.routing), encoding="utf-8")
            output.write_text("existing configuration", encoding="utf-8")
            with patch("config_generator.generator.urlopen", side_effect=URLError("unavailable")):
                with self.assertRaisesRegex(URLError, "unavailable"):
                    self.generator.generate_file(routing, output)
            self.assertEqual(output.read_text(encoding="utf-8"), "existing configuration")
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["apisix.yaml", "routing.yaml"])

    def test_temporary_file_creation_failure_preserves_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "apisix.yaml"
            output.write_text("existing configuration", encoding="utf-8")
            config = self.generator.generate(self.routing, self.spec)
            with patch("config_generator.generator.tempfile.NamedTemporaryFile", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    self.generator.write_config(config, output)
            self.assertEqual(output.read_text(encoding="utf-8"), "existing configuration")
            self.assertEqual(list(output.parent.iterdir()), [output])

    def test_atomic_replace_failure_cleans_up_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "apisix.yaml"
            output.write_text("existing configuration", encoding="utf-8")
            config = self.generator.generate(self.routing, self.spec)
            with patch.object(Path, "replace", side_effect=PermissionError("read only")) as replace:
                with self.assertRaisesRegex(PermissionError, "read only"):
                    self.generator.write_config(config, output)
                replace.assert_called_once_with(output)
            self.assertEqual(output.read_text(encoding="utf-8"), "existing configuration")
            self.assertEqual(list(output.parent.iterdir()), [output])


if __name__ == "__main__":
    unittest.main()
