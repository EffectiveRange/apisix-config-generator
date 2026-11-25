[![CI](https://github.com/EffectiveRange/apisix-config-generator/actions/workflows/ci.yaml/badge.svg)](https://github.com/EffectiveRange/apisix-config-generator/actions/workflows/ci.yaml)
[![Coverage badge](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/EffectiveRange/apisix-config-generator/python-coverage-comment-action-data/endpoint.json)](https://htmlpreview.github.io/?https://github.com/EffectiveRange/apisix-config-generator/blob/python-coverage-comment-action-data/htmlcov/index.html)

# apisix-config-generator

A Python library for generating APISIX routing configuration from OpenAPI specification

## Python API

`ConfigGenerator` is an abstract base class with a typed
`generate(routing, spec) -> StandaloneConfig` interface.
`OpenapiConfigGenerator` implements it for OpenAPI 3.x documents:

```python
from pathlib import Path
from config_generator import ConfigGenerator, OpenapiConfigGenerator

generator: ConfigGenerator = OpenapiConfigGenerator()
routing = generator.load_document(Path("routing.yaml"))
spec = generator.load_document(Path("openapi.yaml"))
config = generator.generate(routing, spec)
generator.write_config(config, Path("apisix.yaml"))
```

Alternatively, `generator.generate_file(Path("routing.yaml"), Path("apisix.yaml"))`
loads the specification relative to the routing file (or from its HTTP(S) URL)
and atomically writes the configuration. Existing `generate` and `load_document`
functions remain available as compatibility wrappers.

## Runtime upstream addresses

Services accept literal HTTP(S) origins, such as `http://camera-service:8080`,
or APISIX environment placeholders:

```yaml
api:
  spec: openapi.yaml
routes:
  getCamera:
    service: camera
services:
  camera:
    url: "http://${{CAMERA_SERVICE_ADDR}}"
```

Generate the standalone configuration in CI:

```sh
python config_generator/generator.py --routing routing.yaml --output apisix.yaml
```

The generated upstream uses `scheme: http` and
`nodes: {"${{CAMERA_SERVICE_ADDR}}": 1}`. Use `https://` for `scheme: https`.
Placeholders must reference the entire address and use a variable name matching
`[A-Za-z_][A-Za-z0-9_]*`; appended ports, paths, queries and fragments are not supported.
The generator preserves placeholders without reading its environment.

Pass the address into the APISIX container through Docker Compose:

```yaml
environment:
  CAMERA_SERVICE_ADDR: ${CAMERA_SERVICE_ADDR:?CAMERA_SERVICE_ADDR must be set}
```

Set `CAMERA_SERVICE_ADDR=camera-service:8080` on the host. Its value must be
`host:port`, not a full URL. APISIX resolves it natively in file-driven standalone
mode (`deployment.role: data_plane` and
`deployment.role_data_plane.config_provider: yaml`); no plugin or deployment-time
Python rendering is needed. Mount the generated file as `conf/apisix.yaml`,
including its generated `#END` marker.

Confirm environment interpolation support against your pinned APISIX version
using the [standalone deployment documentation](https://apisix.apache.org/docs/apisix/deployment-modes/#to-config_provider-yaml).
Recreate the container when changing its environment.
