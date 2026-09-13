import assert from "node:assert/strict";
import test from "node:test";

import { archiveCopy } from "../src/features/archive/copy.ts";
import { productCopy } from "../src/lib/product-copy.ts";

test("the product copy describes the evidence contract", () => {
  assert.equal(productCopy.name, "Investment Research Archive");
  assert.match(productCopy.description, /verifiable citations/i);
});

test("the archive copy states the accepted M0.0 fixture boundary", () => {
  assert.match(archiveCopy.safetyNote, /included synthetic fixture only/i);
  assert.match(archiveCopy.upload.fileHint, /included synthetic PDF fixture/i);
});
