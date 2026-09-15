export const archiveCopy = {
  eyebrow: "M0.0 local proof",
  heading: "Research answers should open back to the source.",
  introduction:
    "Import the synthetic earnings brief, process it locally, and ask an evidence-grounded question.",
  workspaceLabel: "Evidence workspace",
  localOnly: "Local-only · Synthetic fixture · English archive",
  safetyNote: "Accepted M0.0 workflow: included synthetic fixture only.",
  steps: ["Import", "Process", "Research", "Open citation"],
  upload: {
    eyebrow: "Step 1",
    heading: "Import the research document",
    description:
      "The original PDF is preserved before automatic background processing creates searchable text.",
    fileLabel: "PDF document",
    fileHint:
      "Use the included synthetic PDF fixture for the accepted M0.0 workflow.",
    selectedFile: "Selected PDF",
    titleLabel: "Document title",
    titlePlaceholder: "Northstar Grid Analytics Ltd. - FY2025 Earnings Brief",
    publicationDateLabel: "Publication date",
    submitIdle: "Import document",
    submitBusy: "Importing…",
  },
  processing: {
    eyebrow: "Step 2",
    heading: "Process the captured document",
    idle: "Import a document to create a processing run.",
    waiting:
      "Processing continues automatically in the background. This status updates while the worker handles the document.",
    refresh: "Check status",
    checking: "Checking…",
    runLabel: "Processing run",
    captured: "Captured and queued for automatic processing.",
    parsing: "The isolated parser is extracting searchable text.",
    ready: "Searchable evidence is ready.",
    failed: "Processing stopped safely.",
  },
  research: {
    eyebrow: "Step 3",
    heading: "Ask the archive",
    description:
      "The answer is limited to validated evidence from this processed document.",
    questionLabel: "Research question",
    answerableExample: "Use answerable example",
    unanswerableExample: "Use insufficient-evidence example",
    submitIdle: "Research document",
    submitBusy: "Researching…",
    progress: {
      RETRIEVING: "Finding eligible passages in the document…",
      GENERATING: "Drafting statements from the retrieved evidence…",
      VALIDATING: "Checking that every statement has valid evidence…",
    },
    resultLabel: "Validated answer",
    coverageLabel: "Evidence coverage",
    insufficientLabel: "Insufficient evidence",
    failedLabel: "Research stopped safely",
  },
  citation: {
    eyebrow: "Step 4",
    supports: "Supporting evidence",
    conflicts: "Conflicting evidence",
    resolving: "Resolving exact source…",
    pageLabel: "Page",
    openOriginal: "Open original page",
  },
  errors: {
    missingFile: "Choose a PDF before importing.",
    unexpected: "The local application could not complete this request.",
  },
} as const;

export const fixtureDefaults = {
  title: "Northstar Grid Analytics Ltd. - FY2025 Earnings Brief",
  language: "en",
  answerableQuestion:
    "What was Northstar Grid Analytics' FY2025 revenue, and how did it change from FY2024?",
  unanswerableQuestion:
    "What dividend did Northstar Grid Analytics declare for FY2025?",
} as const;
