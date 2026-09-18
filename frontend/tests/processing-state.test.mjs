import assert from "node:assert/strict";
import test from "node:test";

import {
  canResearchProcessingRun,
  shouldPollProcessing,
} from "../src/features/archive/processing-state.ts";

test("non-terminal processing states keep polling and block research", () => {
  for (const state of [
    undefined,
    "CAPTURED",
    "PARSING",
    "LEXICAL_READY",
    "INDEXING",
  ]) {
    assert.equal(shouldPollProcessing(state), true);
    assert.equal(canResearchProcessingRun(state), false);
  }
});

test("SEARCHABLE stops polling and enables research", () => {
  assert.equal(shouldPollProcessing("SEARCHABLE"), false);
  assert.equal(canResearchProcessingRun("SEARCHABLE"), true);
});

test("FAILED stops polling without enabling research", () => {
  assert.equal(shouldPollProcessing("FAILED"), false);
  assert.equal(canResearchProcessingRun("FAILED"), false);
});
