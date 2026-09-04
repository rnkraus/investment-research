import assert from "node:assert/strict";
import test from "node:test";

import { productCopy } from "../src/lib/product-copy.ts";

test("the product copy describes the evidence contract", () => {
  assert.equal(productCopy.name, "Investment Research Archive");
  assert.match(productCopy.description, /verifiable citations/i);
});
