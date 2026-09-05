# Identity and Workspace

This module owns the trusted identity and workspace scope used by every product operation. M0.0 uses one fixed local identity; later authentication replaces the provider behind the same application-owned boundary.

## Contents

| File             | Responsibility                                                                    |
| ---------------- | --------------------------------------------------------------------------------- |
| `domain.py`      | Defines the immutable `WorkspaceContext` containing `user_id` and `workspace_id`. |
| `application.py` | Defines `IdentityContextProvider` and its fixed local M0.0 implementation.        |

## Resolution flow

```mermaid
flowchart LR
    REQUEST[FastAPI request] --> DEP[WorkspaceContextDependency]
    DEP --> CONTAINER[ApplicationContainer]
    CONTAINER --> PROVIDER[IdentityContextProvider]
    PROVIDER --> CONTEXT[WorkspaceContext]
    CONTEXT --> OPERATION[Scoped operation]
```

The provider accepts no client workspace value. A query parameter, request body field, or arbitrary UUID therefore cannot change the effective M0.0 workspace. Repository methods introduced in Step 3 must still require this explicit context; possessing an identifier alone never proves authorization.

The fixed provider has no production authentication guarantee. M1 replaces it with an authenticated server-side implementation without changing downstream consumers.
