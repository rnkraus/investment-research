import assert from "node:assert/strict";
import test from "node:test";

import { formatSourceAttribution } from "../src/features/archive/source-attribution.ts";

test("source attribution uses either field and adds exactly one @ prefix", () => {
  assert.equal(formatSourceAttribution({}), undefined);
  assert.equal(
    formatSourceAttribution({ author_name: "Northstar Research" }),
    "Northstar Research",
  );
  assert.equal(
    formatSourceAttribution({ x_handle: "NorthstarIR" }),
    "@NorthstarIR",
  );
  assert.equal(
    formatSourceAttribution({
      author_name: "Northstar Research",
      x_handle: "@NorthstarIR",
    }),
    "Northstar Research (@NorthstarIR)",
  );
});
