export const archiveCopy = {
  eyebrow: "M1 local research slice",
  heading: "Research answers should open back to the source.",
  introduction:
    "Import an English PDF or paste English research text, process it locally, and ask an evidence-grounded question.",
  workspaceLabel: "Evidence workspace",
  localOnly: "Local-only · Single workspace · English archive",
  safetyNote: "M1 workflow: use public, synthetic, or non-sensitive research.",
  steps: ["Import", "Process", "Research", "Open citation"],
  upload: {
    eyebrow: "Step 1",
    heading: "Import research evidence",
    description:
      "The PDF or pasted text is preserved as an immutable original before background processing creates searchable evidence.",
    modeLegend: "Source type",
    pdfMode: "Upload PDF",
    textMode: "Paste text",
    fileLabel: "PDF document",
    fileHint:
      "Choose an English PDF that is public, synthetic, or otherwise non-sensitive.",
    selectedFile: "Selected PDF",
    textLabel: "Research text",
    textHint: "Paste up to 100,000 characters of English research text.",
    textPlaceholder: "Paste the source text exactly as it should be preserved…",
    titleLabel: "Document title",
    titlePlaceholder: "Company and research document title",
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
    lexicalReady:
      "Text extraction is complete. Dense indexing will start next.",
    indexing: "The extracted evidence is being added to the search index.",
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
    modelSettingsLabel: "AI models",
    generationModelLabel: "Answer model",
    generationModelHint: "Drafts the answer from selected source passages.",
    supportModelLabel: "Evidence-check model",
    supportModelHint: "Checks whether each answer statement is supported.",
    terraModel: "GPT-5.6 Terra",
    lunaModel: "GPT-5.6 Luna",
    modelOptionsLoading: "Loading available models…",
    modelOptionsUnavailable:
      "Model choices could not be loaded. Research is paused until they are available.",
    retryModelOptions: "Try loading models again",
    deterministicMode:
      "This local runtime uses deterministic research; AI model selection is unavailable.",
    submitIdle: "Research document",
    submitBusy: "Researching…",
    progressHeading: "Research progress",
    starting: "Starting research…",
    progress: {
      PREPARING_QUESTION: "Preparing the question for search…",
      FINDING_PASSAGES: "Finding relevant passages…",
      DRAFTING_ANSWER: "Drafting an answer from the selected passages…",
      CHECKING_EVIDENCE: "Checking the answer and its evidence…",
    },
    resultLabel: "Validated answer",
    coverageLabel: "Evidence coverage",
    partialCoverageNote:
      "The available evidence does not fully answer the question.",
    insufficientLabel: "Insufficient evidence",
    failedLabel: "Research stopped safely",
  },
  citation: {
    eyebrow: "Step 4",
    supports: "Supporting evidence",
    conflicts: "Conflicting evidence",
    resolving: "Resolving exact source…",
    pageLabel: "Page",
    textRangeLabel: "Characters",
    openOriginal: "Open original source",
  },
  source: {
    eyebrow: "Verified source",
    heading: "Open the evidence in its immutable original.",
    loading: "Resolving and verifying the cited source…",
    pdfHeading: "Original PDF",
    textHeading: "Original pasted text",
    exactEvidence: "Exact cited evidence",
    pageLabel: "Physical PDF page",
    textRangeLabel: "Canonical character range",
    backToArchive: "Back to research archive",
    unavailable:
      "This source could not be verified or is no longer available in this workspace.",
  },
  errors: {
    missingFile: "Choose a PDF before importing.",
    missingText: "Paste research text before importing.",
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
