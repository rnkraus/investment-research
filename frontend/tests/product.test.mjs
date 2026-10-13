import assert from "node:assert/strict";
import test from "node:test";

import { archiveCopy } from "../src/features/archive/copy.ts";
import { productCopy } from "../src/lib/product-copy.ts";

test("the product copy describes the evidence contract", () => {
  assert.equal(productCopy.name, "Investment Research Archive");
  assert.match(productCopy.description, /verifiable citations/i);
});

test("the archive copy states the local M1 input and data boundaries", () => {
  assert.match(archiveCopy.safetyNote, /public, synthetic, or non-sensitive/i);
  assert.match(archiveCopy.upload.pdfMode, /PDF/i);
  assert.match(archiveCopy.upload.textMode, /text/i);
});

test("the processing copy describes automatic background execution", () => {
  const processingCopy = Object.values(archiveCopy.processing).join(" ");

  assert.match(processingCopy, /automatically in the background/i);
  assert.match(archiveCopy.processing.captured, /queued/i);
  assert.match(archiveCopy.processing.lexicalReady, /dense indexing/i);
  assert.match(archiveCopy.processing.ready, /searchable evidence is ready/i);
  assert.doesNotMatch(processingCopy, /separate terminal|copy command/i);
});

test("partial coverage copy explains that the answer remains incomplete", () => {
  assert.match(
    archiveCopy.research.partialCoverageNote,
    /does not fully answer/i,
  );
});

test("workspace copy names tasks and optional details instead of numbered demo steps", () => {
  assert.equal(archiveCopy.workspace.addSource, "Add source");
  assert.equal(archiveCopy.research.modelSettingsLabel, "Model settings");
  assert.equal(archiveCopy.research.detailsLabel, "Research details");
  assert.doesNotMatch(
    JSON.stringify(archiveCopy),
    /Step [1-4]|M1 workflow|M1 local research slice/,
  );
});
