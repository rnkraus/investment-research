export const archiveCopy = {
  eyebrow: "M1 local research slice",
  heading: "Research answers should open back to the source.",
  introduction:
    "Ask a question about saved English research sources, or import a new PDF or text source to expand the archive.",
  workspaceLabel: "Evidence workspace",
  localOnly: "Local-only · Single workspace · English archive",
  safetyNote: "M1 workflow: use public, synthetic, or non-sensitive research.",
  steps: [
    "Ask the archive",
    "Import evidence",
    "Process evidence",
    "Open citation",
  ],
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
  catalog: {
    eyebrow: "Saved evidence",
    heading: "Documents in your archive",
    description:
      "Open any saved original, or select searchable documents to focus your next question.",
    loading: "Loading saved documents…",
    refresh: "Refresh documents",
    hideDocuments: "Hide documents",
    showDocuments: "Show documents",
    clearSelection: "Clear selection",
    deleteSelected: "Delete selected",
    deletionHint:
      "Delete only explicitly selected documents. Permanent deletion also removes complete saved answers that used them. There is no restore.",
    loadMore: "Load more documents",
    empty:
      "No documents saved yet. Import a PDF or paste text below to get started.",
    unavailable:
      "Saved documents could not be loaded. Try refreshing the list.",
    openOriginal: "Open original",
    selectDocument: "Select document",
    selected: "Selected documents",
    selectionRequired:
      "Research requires a non-empty selection containing only Searchable documents, or all saved documents.",
    selectionReset:
      "The document list was refreshed. Select your documents again before asking a focused question.",
    selectionLimit: "You can select up to 100 documents at a time.",
    selectionHint:
      "Select Searchable documents for research. Failed or unprocessed documents can be selected for deletion only; documents still processing cannot be selected. Hiding the list or loading more keeps selection; refreshing clears it. Dates are shown in UTC.",
    unknownDate: "Unknown",
    published: "Published",
    captured: "Captured",
    available: "Available",
    notProcessed: "No processing status",
    statuses: {
      CAPTURED: "Queued",
      PARSING: "Parsing",
      LEXICAL_READY: "Text extracted",
      INDEXING: "Indexing",
      SEARCHABLE: "Searchable",
      FAILED: "Processing failed",
    },
    originalEyebrow: "Saved original",
    originalHeading: "Original document",
    originalLoading: "Loading the integrity-checked original…",
    originalNotice:
      "This is the complete saved original, not a validated answer or a highlighted citation.",
    originalUnavailable:
      "This original could not be opened or is no longer available in this workspace.",
    openPdf: "Open PDF in a separate tab",
  },
  deletion: {
    heading: "Permanently delete selected documents?",
    warning:
      "This cannot be undone. All versions, originals, search data, and complete stored answers that used these documents will be removed, including answers that also used other documents.",
    boundary:
      "Usage accounting is retained. Previously downloaded copies, backups, local diagnostic output, and copies already sent to AI providers are not erased by this action.",
    confirm: "Permanently delete",
    cancel: "Cancel",
    checking: "Checking deletion status…",
    automaticallyUpdating:
      "Status updates automatically every five seconds for up to five minutes. You can continue using the archive.",
    automaticUpdatesStopped:
      "Automatic status updates have stopped. The count may be out of date; accepted cleanup continues independently. Check deletion status to refresh the count and resume automatic updates.",
    trackingLifetime:
      "Keep this page open to track or retry this batch. Reloading does not stop accepted deletion, but this page does not rediscover earlier batches.",
    tracking: "Permanent deletion",
    pending:
      "Accepted documents are excluded from the archive and new research. Cleanup is not yet confirmed complete. You can check again or retry the same confirmed deletion.",
    uncertain:
      "Some deletion statuses could not be confirmed. A missing response does not mean nothing was deleted. Check status before retrying; retry uses only the same confirmed document IDs.",
    dispatchPending:
      "Some cleanup jobs could not be queued. Retry the confirmed deletion to send them again.",
    rejected:
      "The server rejected this batch. No deletion was accepted by this request. Refresh the list before selecting again.",
    completed:
      "Cleanup completed for all confirmed documents, including affected stored answers and canonical search data.",
    progress: "Confirmed cleanup",
    check: "Check deletion status",
    retry: "Retry confirmed deletion",
    close: "Close deletion status",
    selection: "Documents to delete",
    clearedAnswer:
      "The previous answer view was cleared because deletion can invalidate its sources.",
  },
  research: {
    eyebrow: "Research saved evidence",
    heading: "Ask the archive",
    description:
      "Search already saved PDFs and text without importing again. Answers use only validated evidence from eligible sources in this workspace.",
    scopeLegend: "Search scope",
    archiveScope: "All saved documents",
    documentScope: "Selected documents",
    scopeHint:
      "Only current, eligible evidence in this workspace is searched. The server checks source eligibility again for every question.",
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
    submitIdle: "Ask the archive",
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
