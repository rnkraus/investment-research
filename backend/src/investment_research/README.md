# Application Package

`investment_research` is the shared modular-monolith package. Entry points assemble dependencies here and invoke typed application boundaries; they do not own business state.

## Contents

| Path           | Responsibility                                                                                                         |
| -------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `main.py`      | Creates the FastAPI application, attaches the dependency container, registers routes, and installs OpenAPI generation. |
| `bootstrap.py` | Composition root that selects concrete implementations for application-owned contracts.                                |
| `settings.py`  | Validates non-secret local runtime configuration.                                                                      |
| `api/`         | HTTP transport models, dependencies, OpenAPI generation, and routes.                                                   |
| `modules/`     | Business capabilities with explicit ownership and dependency boundaries.                                               |

## Startup flow

```mermaid
flowchart TD
    SETTINGS[Validated settings] --> MAIN[create_app]
    MAIN --> BOOT[build_container]
    BOOT --> STATE[Application container]
    MAIN --> ROUTES[HTTP routes]
    MAIN --> OPENAPI[OpenAPI contract]
    STATE --> ROUTES
```

`main.py` and `bootstrap.py` are the outer composition layer. Module domain and application files must remain independent of this layer. Application startup must never create or mutate database schemas.

The package follows the boundaries in [`docs/02-system-design.md`](../../../docs/02-system-design.md#4-component-boundaries).
