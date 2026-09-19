# Generation Infrastructure

This package contains answer-generation adapters that implement the Research module's
application-owned `AnswerGenerator` port. Generators may propose statements and source references,
but they cannot validate, persist, or publish them.

## Contents

| File               | Responsibility                                                                       |
| ------------------ | ------------------------------------------------------------------------------------ |
| `__init__.py`      | Identifies the generation-adapter package without exporting an implementation.       |
| `deterministic.py` | Implements the bounded synthetic M0.0 fixture case without a model or network call.  |
| `openai.py`        | Implements the bounded M1 typed Responses API adapter behind the same application port. |

## Trust boundary

```mermaid
flowchart LR
    APP[Research application] --> PORT[AnswerGenerator port]
    FIXTURE[Deterministic fixture adapter] -. implements .-> PORT
    OPENAI[Bounded OpenAI adapter] -. implements .-> PORT
    SOURCES[Ordered retrieval sources] --> PORT
    PORT --> PROPOSAL[Unvalidated generated answer]
    PROPOSAL --> VALIDATOR[Server-side evidence validator]
    VALIDATOR -->|accepted only| OUTPUT[Persisted and publishable answer]
```

Pydantic validation makes the generator result schema-valid: fields have bounded types, evidence
offsets have the proposed text's length, and insufficient coverage contains no statements. It does
not make the result evidence-valid. The later validator must reload canonical records and prove the
workspace, active document version, retrieval-candidate membership, exact text and hash, statement
support, and answer coverage before persistence or output.

## M0.0 deterministic adapter

`DeterministicFixtureAnswerGenerator` recognizes one configured synthetic acceptance question. It
returns the configured statement only when the configured evidence text occurs exactly in an
ordered retrieval source. The proposed citation retains the selected candidate identifier and the
exact range within that source. An unknown question or missing passage returns explicit
`INSUFFICIENT` coverage with no statements.

`M0_0_FIXTURE_ANSWER_CASE` is the typed runtime configuration for the one answerable proof case, and
`M0_0_INSUFFICIENT_MESSAGE` is the application-owned safe fallback text. Their values mirror
[`fixtures/m0_0/manifest.yaml`](../../../../../fixtures/m0_0/manifest.yaml), which remains the
fixture and acceptance specification. M0.0 does not add a runtime YAML dependency merely to build
the deterministic adapter; the final acceptance suite is responsible for detecting manifest drift.

This adapter is deliberately M0.0-only. A later approved AI-provider adapter may replace it behind
the same port, while deterministic tests and server-side evidence validation remain.

## M1 OpenAI adapter

`OpenAIAnswerGenerator` makes one foreground, retry-free Responses API call with
`gpt-5.6-terra`, low reasoning, `store=false`, no tools, strict structured output, a 60-second
operation timeout, and at most 2,000 output tokens. Its application contract admits no more than
eight selected sources, 24,000 combined source characters, eight statements, and two evidence
references per statement.

```mermaid
flowchart LR
    INPUT[Bounded AnswerGenerationInput] --> ALLOWLIST[Request ID, question,<br/>candidate ID, source text]
    ALLOWLIST --> API[OpenAI Responses API<br/>no tools, no storage]
    API --> SCHEMA[Strict Pydantic schema]
    SCHEMA --> PROPOSAL[UnvalidatedGeneratedAnswer]
    PROPOSAL --> CANONICAL[Canonical evidence validation]
```

The serialized provider payload omits document, version, chunk, and structure-block identifiers
because the model needs only the candidate identifier that it may cite. Instructions explicitly
treat the question and source text as untrusted data and forbid general-knowledge completion.
Provider connection, configuration, schema, incomplete-output, and refusal failures become safe
application-owned errors without preserving provider messages as public details.

The adapter does not yet select credentials or replace the deterministic generator by itself.
Process settings and composition choose the implementation in a later work block. Normal CI keeps
using recording or deterministic adapters and performs no provider network calls.
