export const archiveCopy = {
  eyebrow: "M0.0 local proof",
  heading: "Research answers should open back to the source.",
  introduction:
    "Import the synthetic earnings brief, process it locally, and ask an evidence-grounded question.",
  workspaceLabel: "Evidence workspace",
  localOnly: "Local-only · Synthetic fixture · English archive",
  safetyNote: "Public or synthetic documents only during M0.0.",
  steps: ["Import", "Process", "Research", "Open citation"],
  upload: {
    eyebrow: "Step 1",
    heading: "Import the research document",
    description:
      "The original PDF is preserved before a separate local runner creates searchable text.",
    fileLabel: "PDF document",
    fileHint: "PDF only, up to the configured local upload limit.",
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
      "After import, run the bounded local processor in a separate terminal. This keeps PDF parsing outside the API process.",
    commandLabel: "Local processing command",
    copyCommand: "Copy command",
    copiedCommand: "Copied",
    refresh: "Check status",
    checking: "Checking…",
    runLabel: "Processing run",
    captured: "Captured and waiting for the local processor.",
    parsing: "The local processor is extracting searchable text.",
    ready: "Searchable evidence is ready.",
    failed: "Processing stopped safely.",
  },
  errors: {
    missingFile: "Choose a PDF before importing.",
    unexpected: "The local application could not complete this request.",
  },
} as const;

export const fixtureDefaults = {
  title: "Northstar Grid Analytics Ltd. - FY2025 Earnings Brief",
  language: "en",
} as const;
