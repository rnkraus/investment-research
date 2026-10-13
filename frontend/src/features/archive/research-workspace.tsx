"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";

import { productCopy } from "@/lib/product-copy";

import { ArchiveResearch } from "./archive-research";
import { archiveCopy } from "./copy";
import { DocumentWorkflow } from "./document-workflow";

export function ResearchWorkspace() {
  const importPanelId = useId();
  const importPanel = useRef<HTMLDivElement>(null);
  const addSourceButton = useRef<HTMLButtonElement>(null);
  const [isImportOpen, setIsImportOpen] = useState(false);
  const [archiveRevision, setArchiveRevision] = useState(0);
  const refreshArchive = useCallback(() => {
    setArchiveRevision((current) => current + 1);
  }, []);

  useEffect(() => {
    if (isImportOpen) {
      importPanel.current?.focus();
    }
  }, [isImportOpen]);

  function closeImport() {
    setIsImportOpen(false);
    addSourceButton.current?.focus();
  }

  return (
    <>
      <a
        className="sr-only focus:not-sr-only focus:mb-4 focus:block focus:rounded-lg focus:bg-white focus:p-3 focus:text-teal-800"
        href="#research-heading"
      >
        {archiveCopy.workspace.skipToResearch}
      </a>
      <header className="mb-6 flex flex-wrap items-center justify-between gap-4 border-b border-slate-200 pb-5">
        <div className="flex items-center gap-3">
          <span
            aria-hidden="true"
            className="grid size-10 shrink-0 place-items-center rounded-xl bg-slate-950 text-sm font-bold text-white"
          >
            IR
          </span>
          <div>
            <h1 className="text-sm font-semibold tracking-tight text-slate-950 sm:text-base">
              {productCopy.name}
            </h1>
            <p className="mt-0.5 text-xs text-slate-500">
              {archiveCopy.workspaceLabel}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <span className="hidden text-xs text-slate-500 sm:block">
            {archiveCopy.localOnly}
          </span>
          <button
            ref={addSourceButton}
            aria-controls={importPanelId}
            aria-expanded={isImportOpen}
            className="rounded-xl bg-teal-700 px-4 py-2.5 text-sm font-semibold text-white hover:bg-teal-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-700"
            onClick={() => {
              if (isImportOpen) {
                closeImport();
              } else {
                setIsImportOpen(true);
              }
            }}
            type="button"
          >
            {archiveCopy.workspace.addSource}
          </button>
        </div>
      </header>

      <div
        ref={importPanel}
        hidden={!isImportOpen}
        id={importPanelId}
        tabIndex={-1}
      >
        <DocumentWorkflow
          onArchiveChanged={refreshArchive}
          onClose={closeImport}
        />
      </div>

      <ArchiveResearch refreshKey={String(archiveRevision)} />
    </>
  );
}
