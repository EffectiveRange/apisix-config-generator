[![CI](https://github.com/EffectiveRange/apisix-config-generator/actions/workflows/ci.yaml/badge.svg)](https://github.com/EffectiveRange/apisix-config-generator/actions/workflows/ci.yaml)
[![Coverage badge](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/EffectiveRange/apisix-config-generator/python-coverage-comment-action-data/endpoint.json)](https://htmlpreview.github.io/?https://github.com/EffectiveRange/apisix-config-generator/blob/python-coverage-comment-action-data/htmlcov/index.html)

# apisix-config-generator

A Python library for generating APISIX routing configuration from OpenAPI specification

## Example

Look at this example OpenAPI specification in [examples/openapi.yaml](examples/openapi.yaml):

```yaml
openapi: 3.1.0
info:
  title: Camera API
  version: 1.0.0
paths:
  /cameras/{cameraId}:
    get:
      operationId: getCamera
      parameters:
        - name: cameraId
          in: path
          required: true
          schema:
            type: string
      responses:
        "200":
          description: Camera details
```

Select the operation by `operationId` and its upstream service in [examples/routing.yaml](examples/routing.yaml), beside the spec:

```yaml
api:
  spec: openapi.yaml
routes:
  getCamera:
    service: camera-service
services:
  camera-service:
    url: http://camera-service:8080
plugins:
  oas-validator:
    spec_url: https://www.example.com/openapi.json
    verbose_errors: true
```

Generate the standalone APISIX configuration:

```sh
apisix-config-generator --routing examples/routing.yaml --output examples/apisix.yaml
```

The resulting [examples/apisix.yaml](examples/apisix.yaml) contains:

```yaml
routes:
  - id: getCamera
    uri: /cameras/:cameraId
    methods:
      - GET
    upstream:
      type: roundrobin
      scheme: http
      nodes:
        camera-service:8080: 1
      pass_host: pass
    plugins:
      oas-validator:
        spec_url: https://www.example.com/openapi.json
        verbose_errors: true
#END
```

The `operationId` selects the route, `{cameraId}` becomes an APISIX path
parameter, and the service URL supplies the upstream scheme and `host:port`.
Only operations listed in `routing.yaml` are exposed.

## Python API

`ConfigGenerator` is an abstract base class with a typed
`generate(routing, spec) -> StandaloneConfig` interface.
`OpenapiConfigGenerator` implements it for OpenAPI 3.x documents:

```python
from pathlib import Path
from config_generator import ConfigGenerator, OpenapiConfigGenerator, OpenapiSecurityProvider

generator: ConfigGenerator = OpenapiConfigGenerator(OpenapiSecurityProvider())
routing = generator.load_document(Path("routing.yaml"))
spec = generator.load_document(Path("openapi.yaml"))
config = generator.generate(routing, spec)
generator.write_config(config, Path("apisix.yaml"))
```

Alternatively, `generator.generate_file(Path("routing.yaml"), Path("apisix.yaml"))`
loads the specification relative to the routing file (or from its HTTP(S) URL)
and atomically writes the configuration. Existing `generate` and `load_document`
functions remain available as compatibility wrappers.

Security handling is implemented separately in `config_generator/security.py`.
`SecurityProvider` is the abstract interface; `OpenapiSecurityProvider` implements
the OpenAPI-to-APISIX authentication mapping. Inject a provider through the
constructor to replace security handling:

```python
from config_generator import OpenapiConfigGenerator, OpenapiSecurityProvider, SecurityProvider

security: SecurityProvider = OpenapiSecurityProvider()
generator = OpenapiConfigGenerator(security_provider=security)
```

The constructor requires a `SecurityProvider` instance and does not select an
implementation. The CLI and package-level compatibility functions compose the
generator with `OpenapiSecurityProvider`. Providers receive the operation, routing descriptor, API spec,
operation ID and merged route plugins; they return security plugins to merge and
raise `ConfigError` for unsupported requirements or conflicts. The generator
continues to log and re-raise generation errors.

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
apisix-config-generator --routing routing.yaml --output apisix.yaml
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

## Authenticate API requests with authentik

OpenAPI `security` requirements generate an APISIX
[`openid-connect` plugin](https://apisix.apache.org/docs/apisix/plugins/openid-connect/)
for secured routes. Clients obtain an **access token** from authentik and send it
as `Authorization: Bearer <access-token>`. By default, APISIX validates JWT tokens
locally using authentik's JWKS: missing or invalid tokens receive `401`,
and tokens without the required scopes receive `403`. This is bearer-only API
authentication, not a browser login redirect flow.

Configure IDP credentials and endpoints in `routing.yaml`, using the same scheme
name as `components.securitySchemes` in OpenAPI:

```yaml
security_schemes:
  authentik:
    client_id: "${{AUTHENTIK_CLIENT_ID}}"
    client_secret: "${{AUTHENTIK_CLIENT_SECRET}}"
    discovery: https://authentik.example.com/application/o/cameras/.well-known/openid-configuration
```

`client_id` and `client_secret` are required. `discovery` is required for `oauth2`
and HTTP bearer schemes; for `openIdConnect`, it defaults to the scheme's
`openIdConnectUrl`. Set `use_jwks: false` to select introspection.
In that mode, `introspection_endpoint` is optional when provided by discovery.
`introspection_endpoint_auth_method` accepts `client_secret_basic` (default) or
`client_secret_post`. Endpoint URLs must use HTTPS, and TLS certificate
verification is always enabled. Complete `${{NAME}}` placeholders are also
accepted for credentials and endpoint URLs; endpoint environment values must
be full HTTPS URLs, unlike upstream address values.

Supported security schemes are `type: oauth2`, `type: openIdConnect`, and
`type: http` with `scheme: bearer`. OAuth2 scopes must be declared in the scheme's
flows. Required OAuth2/OIDC scopes become the plugin's `required_scopes`;
HTTP bearer requirements must use an empty scope list.

API-level `security` applies unless overridden by an operation. `security: []`
or `security: [{}]` makes an operation public, without generating authentication.
If neither level declares security, existing manually configured plugins are
preserved. For declared security, a manually configured `openid-connect` plugin
is rejected to prevent conflicts; configure `security_schemes` instead.
Unrelated global and per-route plugins continue to work.

Only one security requirement containing one scheme is supported per operation.
Alternative requirements (OR), combined schemes (AND), security scheme `$ref`s,
API keys, and other authentication types fail explicitly. Missing IDP settings,
unknown schemes and malformed requirements also fail generation, rather than
silently exposing a secured route.

See the complete [secured OpenAPI spec](examples/authentik-openapi.yaml),
[routing descriptor](examples/authentik-routing.yaml), and
[generated configuration](examples/authentik-apisix.yaml). The example protects
`getCamera` with `cameras:read` and leaves `getHealth` public:

```sh
apisix-config-generator --routing examples/authentik-routing.yaml --output examples/authentik-apisix.yaml
```

Create an authentik OAuth2/OpenID provider associated with the `cameras`
application. Configure an asymmetric signing key, access tokens whose audience
matches `client_id`, and the necessary scope mappings (such as `cameras:read`).
When selecting introspection, use a confidential provider's credentials. Tokens must
be issued by that provider; for tokens from another provider, explicitly grant
cross-provider introspection via authentik's **Federated OAuth2/OpenID Providers**
setting. See [authentik's provider documentation](https://docs.goauthentik.io/add-secure-apps/providers/oauth2/).

Pass credentials into the APISIX container, not the CI generator environment:

```yaml
environment:
  AUTHENTIK_CLIENT_ID: ${AUTHENTIK_CLIENT_ID:?AUTHENTIK_CLIENT_ID must be set}
  AUTHENTIK_CLIENT_SECRET: ${AUTHENTIK_CLIENT_SECRET:?AUTHENTIK_CLIENT_SECRET must be set}
```

Do not commit real credentials. APISIX must be able to reach discovery and
JWKS endpoints (or introspection endpoints when selected), trust their TLS certificates, and have `openid-connect`
enabled. Confirm plugin, `required_scopes` and environment interpolation support
in your pinned APISIX version (the generated settings were checked against the
APISIX 3.14.1 plugin). Recreate the container when changing its environment.

### Local JWT validation with JWKS

JWKS is the default. You can explicitly set `use_jwks: true` to validate signed JWT access tokens
locally instead of using introspection:

```yaml
security_schemes:
  authentik:
    use_jwks: true
    client_id: "${{AUTHENTIK_CLIENT_ID}}"
    client_secret: "${{AUTHENTIK_CLIENT_SECRET}}"
    discovery: https://authentik.example.com/application/o/cameras/.well-known/openid-configuration
```

Omitting `use_jwks` selects JWKS. Set it to `false` to opt into introspection.
Remove `introspection_endpoint` and
`introspection_endpoint_auth_method` when enabling JWKS; conflicting settings
are rejected. `client_secret` remains required by the APISIX plugin schema,
although JWT verification does not use it to call introspection.

APISIX retrieves the JWKS URL and issuer from discovery and caches discovery,
keys and verification results. It does not introspect each token, but still
contacts authentik when discovery or keys need fetching or refreshing.
Configure an asymmetric signing key on the authentik provider and ensure its
JWT access tokens have an `aud` claim containing the configured `client_id`.
JWKS mode requires that audience match, rejects unsigned/unsupported token
algorithms, and retains the OpenAPI scope checks. The token's `scope` claim must
be a space-separated string for those checks. Opaque tokens cannot use this mode.

Revoked tokens can remain usable until expiry: local JWT validation does not
check revocation with authentik. Use short-lived access tokens or introspection
when earlier revocation detection is required. APISIX must trust the IDP's TLS
certificate and support `use_jwks` and `claim_validator` in the pinned version.
