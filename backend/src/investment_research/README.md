# Application Package

`investment_research` is the shared modular-monolith package. Entry points assemble dependencies here and invoke typed application boundaries; they do not own business state.

## Contents

| Path              | Responsibility                                                                                                   |
| ----------------- | ---------------------------------------------------------------------------------------------------------------- |
| `main.py`         | Creates FastAPI, installs transport safeguards and error handlers, attaches the container, and registers routes. |
| `worker.py`       | Creates the Celery worker application and executes validated processing jobs with process-owned resources.       |
| `parser_app.py`   | Hosts the credential-free PDF parser on the private parser network.                                              |
| `bootstrap.py`    | Separate API and worker composition roots that select concrete implementations for application-owned contracts.  |
| `settings.py`     | Loads least-privilege API, worker, parser, and database-maintenance configuration.                               |
| `api/`            | HTTP transport models, dependencies, OpenAPI generation, and routes.                                             |
| `infrastructure/` | Technical database, logging, queue, lexical-search, parsing, and secure local object-storage adapters.           |
| `modules/`        | Business capabilities with explicit ownership and dependency boundaries.                                         |

## Startup flow

```mermaid
flowchart TD
    SETTINGS[ApiSettings] --> MAIN[create_app]
    MAIN --> LIFE[FastAPI lifespan]
    LIFE -->|startup| BOOT[build_api_container]
    BOOT --> ENGINE[SQLAlchemy engine]
    ENGINE --> SESSIONS[Session factory]
    SESSIONS --> CAPTUREUOW[Capture unit-of-work factory]
    SESSIONS --> READUOW[Document read unit-of-work factory]
    SESSIONS --> STATUSUOW[Processing read unit-of-work factory]
    SESSIONS --> RETRIEVALUOW[Research retrieval unit-of-work factory]
    SESSIONS --> ANSWERUOW[Answer validation unit-of-work factory]
    BOOT --> STORAGE[ObjectStorage adapter]
    BOOT --> CELERY[Celery application]
    CELERY --> DISPATCH[CeleryProcessingDispatcher]
    BOOT --> GENERATOR[Deterministic answer generator]
    BOOT --> SUPPORT[Independent statement-support evaluator]
    CAPTUREUOW --> CAPTURE[CaptureDocumentService]
    READUOW --> ORIGINAL[OpenOriginalService]
    STATUSUOW --> STATUS[GetProcessingRunService]
    RETRIEVALUOW --> RETRIEVE[RetrieveResearchService]
    RETRIEVALUOW --> LEXICAL[PostgreSQL LexicalSearch adapter]
    ANSWERUOW --> GENERATE[GenerateValidatedAnswerService]
    GENERATOR --> GENERATE
    SUPPORT --> GENERATE
    RETRIEVE --> ANSWERREQUEST[AnswerResearchRequestService]
    GENERATE --> ANSWERREQUEST
    STORAGE --> CAPTURE
    DISPATCH --> CAPTURE
    STORAGE --> ORIGINAL
    BOOT --> STATE[ApiContainer]
    CELERY --> STATE
    DISPATCH --> STATE
    CAPTURE --> STATE
    ORIGINAL --> STATE
    STATUS --> STATE
    ANSWERREQUEST --> STATE
    MAIN --> LIMIT[Request-body limit]
    LIMIT --> ROUTES[HTTP routes]
    MAIN --> ERRORS[Safe error handlers]
    MAIN --> OPENAPI[OpenAPI contract]
    STATE --> ROUTES
    ROUTES --> ERRORS
    LIFE -->|shutdown| CLOSE[Close Celery resources and engine pool]
```

`main.py` and `bootstrap.py` are the outer composition layer. Module domain and application files must remain independent of this layer. Application startup must never create or mutate database schemas.

The Celery worker is a second trusted process entry point with its own composition root:

```mermaid
flowchart TD
    SETTINGS[WorkerSettings] --> ENTRY[worker.py]
    ENTRY --> CELERY[Celery application]
    ENTRY --> EXECUTOR[WorkerProcessingJobExecutor]
    CELERY --> TASK[Validated ProcessingJobV1 task]
    TASK --> EXECUTOR
    EXECUTOR -->|lazy per worker child| BOOT[build_worker_container]
    BOOT --> DB[(PostgreSQL resources)]
    BOOT --> STORAGE[ObjectStorage adapter]
    BOOT --> PARSER[HttpDocumentParser]
    BOOT --> EMBED[OpenAIEmbeddingProvider]
    BOOT --> DENSE[PineconeDenseIndexWriter]
    BOOT --> SERVICE[ExecuteProcessingJobService]
    DB --> SERVICE
    STORAGE --> SERVICE
    PARSER --> SERVICE
    EMBED --> SERVICE
    DENSE --> SERVICE
    SERVICE --> TERMINAL[SEARCHABLE, safe retry, or FAILED]
    TERMINAL --> CLOSE[Dispose resources on worker-child shutdown]
```

The queue payload uses the versioned `ProcessingJobV1` contract rather than accepting arbitrary
database or session objects. `WorkerProcessingJobExecutor` creates the worker container only when a
child handles its first task, reuses process-owned resources safely, and disposes them when that
child shuts down. Shutdown closes the Pinecone gRPC channel, OpenAI HTTP client, and database engine.
The application service revalidates canonical workspace and run state before it opens the original,
indexes active chunks, or commits derived output.

`ApiSettings` and `WorkerSettings` may load the ignored root development file. The API requires an
authenticated Redis broker URL when its container is built; host development may derive it from
`REDIS_PASSWORD` and `REDIS_PORT`. Optional M1 provider groups are role-specific and fail closed when
partially configured: `ApiSettings.query_providers` accepts only query-embedding and Pinecone-query
credentials, while `WorkerSettings.indexing_providers` accepts only document-embedding and
Pinecone-index credentials. Both retain the frozen model, dimensions, configuration versions, and
timeouts. The worker group is required when its container is built and is wired only to document
indexing; the API query group remains unused until the M1 retrieval block wires its approved
adapters. `ParserSettings` loads only its allowlisted process variables,
exposes no database, queue, or provider field, and rejects known application credential variables.
`runtime_location=host` restricts bind, database, object-storage, and broker targets to loopback;
`runtime_location=container` permits only the fixed internal Compose service targets required by
the topology.

API requests pass through a server-owned correlation boundary before body limits and route
handling. `RequestLoggingMiddleware` replaces any client-supplied request ID, adds the generated UUID
to the response, and emits one completion or failure event. It matches only registered route
templates and never logs raw paths, query strings, headers, or bodies. `SafeJsonFormatter` emits a
fixed field allowlist; arbitrary messages, values, exception details, and traceback text are
discarded rather than copied into logs. Safe API error envelopes carry the same request ID.

Health reporting has two layers. `/health` reads only already-created FastAPI metadata and proves
that the API process can answer HTTP; it never calls a dependency probe. `/ready` asks the
container-owned `CompositeReadinessProbe` whether every required dependency is usable. Its current
`PostgreSQLReadinessProbe` requires both a successful connection and the exact Alembic head expected
by this application version. `CeleryBrokerReadinessProbe` opens one bounded connection through the
same Celery application used for publication. When the configured provider is S3,
`S3ObjectStorageReadinessProbe` verifies access to the canonical-original bucket through the same
client and credentials used by document storage. The local filesystem mode omits only the S3 probe.
The response exposes only `ready` or `not_ready`; safe structured logs retain broad operational
failure categories without connection details.

Importing `create_app` or exporting OpenAPI does not allocate database resources. The FastAPI
lifespan creates them only when the server starts and releases them during shutdown. Sessions are
still created per unit of work; they are never shared across requests or worker executions.

For document capture, the request-body middleware rejects excessive transport payloads before
multipart parsing. The route then resolves the server-owned workspace and capture service from the
application container. The service validates the PDF and coordinates immutable storage with one
database unit of work; the route only translates the typed result or a known safe error.

For original access, the route supplies only server-resolved workspace scope and path identifiers.
The read service resolves an eligible current original through a short-lived non-committing session,
then verifies the stored bytes against canonical size and checksum before allowing the response to
stream them.

For processing status, the route supplies the server-resolved workspace and one run identifier to
`GetProcessingRunService`. A separate short-lived read session resolves the canonical snapshot; the
API returns only its public state and sanitized failure summary and cannot invoke parsing.

For research retrieval, `RetrieveResearchService` creates the immutable request, executes the
eligible PostgreSQL lexical query, and stages the completed run and ordered candidates through one
shared short-lived transaction. `GenerateValidatedAnswerService` is wired with the deterministic
M0.0 generator, an independent support evaluator, and the answer-validation unit of work. It
reloads canonical evidence and persists only a safe terminal aggregate.

`AnswerResearchRequestService` composes those two focused services into the complete application
use case. The research HTTP route calls this single boundary and receives only
`ValidatedAnswerResult`; it does not coordinate retrieval and generation or gain access to
unvalidated generator output. The route serializes the validated terminal aggregate into typed SSE
events and never emits raw generator tokens or validation details.

The package follows the boundaries in [`docs/02-system-design.md`](../../../docs/02-system-design.md#4-component-boundaries).
