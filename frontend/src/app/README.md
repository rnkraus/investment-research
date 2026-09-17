# App Router

This directory owns the Next.js route tree, shared document layout, and global presentation entry point.

## Current files

| File          | Responsibility                                                                       |
| ------------- | ------------------------------------------------------------------------------------ |
| `layout.tsx`  | Defines English document metadata, imports global styles, and renders route content. |
| `page.tsx`    | Composes the local M1 archive shell and implemented research workflow.               |
| `globals.css` | Defines Tailwind CSS integration and global visual defaults.                         |

## Rendering relationship

```mermaid
flowchart TD
    LAYOUT[Root layout] --> METADATA[Product metadata]
    LAYOUT --> STYLES[Global styles]
    LAYOUT --> PAGE[Archive page]
    PAGE --> COPY[Shared and feature copy]
    PAGE --> WORKFLOW[DocumentWorkflow client component]
```

`page.tsx` remains a server component and delegates browser state, PDF or pasted-text capture,
status polling, and evidence-grounded research to the archive feature. Future document and research
pages are added here only when their backend path is ready. Route components may call typed frontend
feature or API functions, but they must not contain backend persistence logic or independently
redefine API payloads.

All V1 interface text remains English. The `lang` metadata stays explicit so additional interface languages can be introduced deliberately later.
